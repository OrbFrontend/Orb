from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from ....core import DecisionDefinition
from ....inference import (
    MAX_QUESTION_BYTES,
    MAX_QUESTIONS_PER_REQUEST,
    MAX_REQUEST_BYTES,
    MAX_STATE_BYTES,
    RAW_ANSWER_CACHE,
    AbortToken,
    CachedAnswer,
    ChoiceAnswer,
    DecisionCancelled,
    DecisionClient,
    DecisionQuestion,
    DecisionResponse,
    DecisionTransportError,
    LLMCallError,
    ScoreAnswer,
    cache_key,
)
from . import cooldown
from .guidance import decision_guidance_block
from .records import (
    envelope,
    invalidated_anchor,
    matching_replay,
    raw_request_fingerprint,
    resolution_policy_fingerprint,
    stored_evaluations,
    stored_skipped,
)
from .render import (
    STATE_MACROS,
    TEXT_MACROS,
    DecisionSnapshot,
    UnavailableMacro,
    definition_macro_errors,
    macros_used,
    render,
)
from .resolve import (
    DRAWN_RESOLUTIONS,
    ROUTINE_REASONS,
    SkipReason,
    draw_uniform,
    gate_holds,
    resolve_argmax,
    resolve_gated,
    resolve_nearest,
    resolve_roll,
    resolve_threshold,
    resolve_weighted,
)

if TYPE_CHECKING:
    from ...state import TurnState

logger = logging.getLogger(__name__)

# Also keeps every exchange within the gateway's MAX_QUESTIONS_PER_REQUEST:
# each decision asks exactly one question.
MAX_DECISIONS_PER_EXCHANGE = 32
MAX_DECISIONS_PER_CARD = 8
# Batches go out concurrently under one timeout, so this cap and the timeout are
# the whole stage budget: the stage costs at most one REQUEST_TIMEOUT_SECONDS.
MAX_BATCHES = 4
REQUEST_TIMEOUT_SECONDS = 3.0
_SITUATION_MACROS = frozenset({"last_message", "last_assistant_message", "recent_history"})

_EVENT_FIELDS = (
    "fragment_id",
    "fragment_label",
    "injection_label",
    "source",
    "placement",
    "scope",
    "occurrence_id",
    "outcome",
    "guidance",
    "answer_source",
    "replay_invalidated",
    "probability",
    "distribution",
    "confidence",
    "score",
    "draw",
    "elapsed_ms",
)

Answer = float | ChoiceAnswer | ScoreAnswer


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    url: str = ""
    api_key: str = ""
    model: str = ""
    proxy: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.model)


def _source(card_id: str | None) -> str:
    return f"card:{card_id}" if card_id else "global"


@dataclass(frozen=True, slots=True)
class DecisionCandidate:
    definition: DecisionDefinition
    card_id: str | None = None


@dataclass(frozen=True, slots=True)
class InvalidDecision:
    fragment_id: str
    label: str = ""
    card_id: str | None = None


@dataclass(frozen=True, slots=True)
class JudgeTurn:
    snapshot: DecisionSnapshot
    candidates: tuple[DecisionCandidate, ...] = ()
    config: JudgeConfig = field(default_factory=JudgeConfig)
    prior_cooldowns: Mapping[str, int] = field(default_factory=dict)
    replay_records: tuple[Mapping[str, Any], ...] = ()
    invalid: tuple[InvalidDecision, ...] = ()
    # A group's card-embedded decisions read their own character as ``{{char}}``
    # and ``{{description}}``, the way that card's text does in the prompt.
    card_snapshots: Mapping[str, DecisionSnapshot] = field(default_factory=dict)

    def snapshot_for(self, candidate: DecisionCandidate) -> DecisionSnapshot:
        return self.card_snapshots.get(candidate.card_id or "", self.snapshot)


@dataclass(slots=True)
class JudgeResult:
    evaluations: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    cooldowns: dict[str, int] = field(default_factory=dict)
    guidance: str = ""
    requests: int = 0
    inherited: bool = False

    @classmethod
    def committed(cls, stored: Mapping[str, Any] | None, cooldowns: Mapping[str, int]) -> JudgeResult:
        """The result an earlier reply already committed its exchange to, taken as is.

        Nothing is asked and nothing is drawn: a later speaker in the same
        exchange must land on the outcome the earlier speakers already wrote.
        """
        evaluations = stored_evaluations(stored)
        return cls(
            evaluations=evaluations,
            skipped=stored_skipped(stored),
            cooldowns={str(key): int(value) for key, value in cooldowns.items()},
            guidance=decision_guidance_block(evaluations),
            inherited=True,
        )

    def as_event_data(self) -> dict[str, Any]:
        evaluations = [{key: row[key] for key in _EVENT_FIELDS if key in row} for row in self.evaluations]
        data: dict[str, Any] = {"evaluations": evaluations, "skipped": self.skipped, "cooldowns": self.cooldowns}
        if self.inherited:
            data["inherited"] = 1
        return data

    def apply_to(self, state: TurnState) -> None:
        """Carry the result on *state* so persistence commits it in the reply's own INSERT."""
        state.decision_evaluations = envelope(self.evaluations, self.skipped)
        state.decision_cooldowns = dict(self.cooldowns)
        state.decision_guidance = self.guidance


@dataclass(slots=True)
class _Item:
    """One running decision: its rendered question, then its answer or why it has none."""

    candidate: DecisionCandidate
    policy_fingerprint: str
    outputs: dict[str, str] = field(default_factory=dict)
    state: str = ""
    question: DecisionQuestion | None = None
    raw_fingerprint: str = ""
    skip_reason: str = ""
    oversize: tuple[int, int] | None = None
    replay_invalidated: str = ""
    answer: Answer | None = None
    answer_source: str = ""
    replayed_from: str = ""
    returned_model: str = ""
    elapsed_ms: int = 0

    @property
    def definition(self) -> DecisionDefinition:
        return self.candidate.definition


def _eligible(turn: JudgeTurn) -> tuple[list[DecisionCandidate], list[dict[str, Any]]]:
    skipped = [
        {
            "fragment_id": broken.fragment_id,
            "fragment_label": broken.label,
            "source": _source(broken.card_id),
            "reason": SkipReason.INVALID_DEFINITION,
            "failed": 1,
        }
        for broken in turn.invalid
    ]
    running: list[DecisionCandidate] = []
    resting = cooldown.blocked(turn.prior_cooldowns)
    for candidate in turn.candidates:
        if candidate.definition.fragment_id not in resting:
            running.append(candidate)
            continue
        skipped.append(
            {
                "fragment_id": candidate.definition.fragment_id,
                "fragment_label": candidate.definition.label,
                "source": _source(candidate.card_id),
                "reason": SkipReason.RESTING,
            }
        )
    return running, skipped


def _over_budget(running: Sequence[DecisionCandidate]) -> set[str]:
    over: set[str] = set()
    per_card: dict[str, int] = {}
    for index, candidate in enumerate(running):
        if candidate.card_id:
            per_card[candidate.card_id] = per_card.get(candidate.card_id, 0) + 1
        if index >= MAX_DECISIONS_PER_EXCHANGE or (candidate.card_id and per_card[candidate.card_id] > MAX_DECISIONS_PER_CARD):
            over.add(candidate.definition.fragment_id)
    return over


def _prepare(candidate: DecisionCandidate, turn: JudgeTurn, *, over_budget: bool) -> _Item:
    item = _Item(candidate, resolution_policy_fingerprint(candidate.definition, scope=turn.snapshot.scope))
    item.skip_reason = _render_question(item, turn, over_budget=over_budget)
    return item


def _render_question(item: _Item, turn: JudgeTurn, *, over_budget: bool) -> str:
    """Render *item*'s request, or return why it cannot be asked."""
    definition, snapshot = item.definition, turn.snapshot_for(item.candidate)
    if definition_macro_errors(definition):
        return SkipReason.INVALID_DEFINITION
    try:
        item.outputs = {key: render(value, snapshot, allowed=TEXT_MACROS) for key, value in definition.outputs.items()}
    except UnavailableMacro:
        return SkipReason.UNAVAILABLE_CONTEXT
    if over_budget:
        return SkipReason.BUDGET_EXHAUSTED
    if not turn.config.configured:
        return SkipReason.NOT_CONFIGURED
    try:
        state = render(definition.state_template, snapshot, allowed=STATE_MACROS)
        instructions = render(definition.instructions, snapshot, allowed=TEXT_MACROS)
        if isinstance(definition.criteria, Mapping):
            criteria: Mapping[str, str] | tuple[str, ...] = {
                key: render(value, snapshot, allowed=TEXT_MACROS) for key, value in definition.criteria.items()
            }
        else:
            criteria = tuple(render(value, snapshot, allowed=TEXT_MACROS) for value in definition.criteria)
    except UnavailableMacro:
        return SkipReason.UNAVAILABLE_CONTEXT
    used = set(macros_used(definition.state_template))
    if used and used <= _SITUATION_MACROS and not any((snapshot.value(macro) or "").strip() for macro in used):
        return SkipReason.EMPTY_INPUT
    texts = criteria.values() if isinstance(criteria, Mapping) else criteria
    sizes = (len(state.encode()), len(instructions.encode()) + sum(len(text.encode()) for text in texts))
    if sizes[0] > MAX_STATE_BYTES or sizes[1] > MAX_QUESTION_BYTES:
        item.oversize = sizes
        return SkipReason.OVERSIZED_INPUT
    item.state = state
    item.question = DecisionQuestion(definition.fragment_id, instructions, criteria, definition.decision_type)
    item.raw_fingerprint = raw_request_fingerprint(turn.config.model, state, item.question)
    return ""


def _answer_fields(answer: Answer) -> dict[str, Any]:
    if isinstance(answer, (int, float)):
        return {"probability": float(answer)}
    if isinstance(answer, ChoiceAnswer):
        return {"distribution": dict(answer.probabilities), "confidence": answer.confidence, "returned_choice": answer.selected}
    return {
        "distribution": dict(answer.probabilities),
        "confidence": answer.confidence,
        "score": answer.score,
        "legend": dict(answer.legend),
    }


def _resolve_answer(answer: Answer, definition: DecisionDefinition) -> tuple[str, float | None]:
    keys, resolution = definition.outcome_keys, definition.resolution
    if isinstance(answer, (ChoiceAnswer, ScoreAnswer)) and resolution == "gated" and gate_holds(answer.probabilities, keys):
        return keys[0], None  # settled without a draw, so none is recorded
    draw = draw_uniform() if resolution in DRAWN_RESOLUTIONS else None
    if definition.decision_type == "noul" and isinstance(answer, (int, float)):
        if draw is not None:
            return resolve_roll(float(answer), draw), draw
        return resolve_threshold(float(answer), definition.threshold if definition.threshold is not None else 0.5), None
    if isinstance(answer, (ChoiceAnswer, ScoreAnswer)):
        if draw is not None:
            resolve = resolve_gated if resolution == "gated" else resolve_weighted
            return resolve(answer.probabilities, keys, draw), draw
        if definition.resolution == "nearest" and isinstance(answer, ScoreAnswer):
            return resolve_nearest(answer.score, keys), None
        return resolve_argmax(answer.probabilities, keys), None
    raise ValueError("answer primitive did not match its definition")


def _gated(definition: DecisionDefinition, answer: Answer) -> bool:
    confidence = getattr(answer, "confidence", None)
    return definition.confidence_floor is not None and confidence is not None and confidence < definition.confidence_floor


def _resolved_record(item: _Item, turn: JudgeTurn) -> dict[str, Any] | None:
    """The evaluation record, or ``None`` with ``item.skip_reason`` set.

    Returning nothing is the whole point: an unanswered decision contributes no
    outcome and no guidance, and the caller turns it into a skip row.
    """
    definition, question, answer = item.definition, item.question, item.answer
    if question is None or answer is None:
        item.skip_reason = SkipReason.INVALID_ANSWER
        return None
    if _gated(definition, answer):
        item.skip_reason = SkipReason.LOW_CONFIDENCE
        return None
    try:
        outcome, draw = _resolve_answer(answer, definition)
    except ValueError:
        item.skip_reason = SkipReason.INVALID_ANSWER
        return None
    record: dict[str, Any] = {
        "fragment_id": definition.fragment_id,
        "fragment_label": definition.label,
        "injection_label": definition.injection_label,
        "source": _source(item.candidate.card_id),
        "placement": definition.placement,
        "scope": turn.snapshot.scope,
        "occurrence_id": str(uuid.uuid4()),
        "input_branch_anchor": turn.snapshot.anchor_message_id,
        "rendered_state": item.state,
        "rendered_instructions": question.instructions,
        "rendered_criteria": question.criteria,
        "raw_request_fingerprint": item.raw_fingerprint,
        "resolution_policy_fingerprint": item.policy_fingerprint,
        "outputs": item.outputs,
        "requested_model": turn.config.model,
        "returned_model": item.returned_model,
        "elapsed_ms": item.elapsed_ms,
        "outcome": outcome,
        "guidance": item.outputs.get(outcome, ""),
        "answer_source": item.answer_source,
        **_answer_fields(answer),
    }
    if item.replay_invalidated:
        record["replay_invalidated"] = item.replay_invalidated
    if item.replayed_from:
        record["replayed_from"] = item.replayed_from
    if draw is not None:
        record["draw"] = draw
    return record


def _skipped_row(item: _Item) -> dict[str, Any]:
    """The row for a decision that produced nothing: who it was and why it did not run.

    The oversize numbers are the one extra -- they are the only reason an author
    cannot act on without being told the size they overran. ``failed`` separates
    "something broke" from the routine skips an author configured on purpose.
    """
    row: dict[str, Any] = {
        "fragment_id": item.definition.fragment_id,
        "fragment_label": item.definition.label,
        "source": _source(item.candidate.card_id),
        "reason": item.skip_reason,
    }
    if item.skip_reason not in ROUTINE_REASONS:
        row["failed"] = 1
    if item.oversize is not None:
        row["oversize_state_bytes"], row["oversize_question_bytes"] = item.oversize
        row["state_limit"], row["question_limit"] = MAX_STATE_BYTES, MAX_QUESTION_BYTES
    return row


def _origin(stored: Mapping[str, Any]) -> str:
    """Where *stored*'s answer first came from, through any number of regenerations."""
    return str(stored.get("replayed_from") or stored.get("answer_source") or "")


def _stored_answer(stored: Mapping[str, Any], decision_type: str) -> Answer | None:
    """The classifier answer *stored* was resolved from, or ``None`` if it cannot be read back."""
    try:
        if decision_type == "noul":
            return float(stored["probability"])
        probabilities = {str(key): float(value) for key, value in stored["distribution"].items()}
        confidence = float(stored["confidence"])
        if decision_type == "choice":
            return ChoiceAnswer(str(stored["returned_choice"]), probabilities, confidence)
        legend = {str(key): str(value) for key, value in stored["legend"].items()}
        return ScoreAnswer(float(stored["score"]), probabilities, confidence, legend)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _redraw(item: _Item, stored: Mapping[str, Any]) -> bool:
    """Arm *item* with the stored answer so it resolves with a fresh draw.

    A drawn outcome is the dice, not the classifier: keeping it would make every
    regeneration land on the same side of the odds the author asked to roll
    against. The answer is kept, so rerolling costs no request.
    """
    answer = _stored_answer(stored, item.definition.decision_type)
    if answer is None:
        return False
    item.answer, item.answer_source, item.replayed_from = answer, "replay", _origin(stored)
    item.returned_model = str(stored.get("returned_model") or "")
    return True


def _replayed(item: _Item, stored: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **stored,
        "fragment_label": item.definition.label,
        "injection_label": item.definition.injection_label,
        "source": _source(item.candidate.card_id),
        "outputs": item.outputs,
        "answer_source": "replay",
        "replayed_from": _origin(stored),
        "guidance": item.outputs.get(str(stored.get("outcome") or ""), ""),
    }


def _cache_key(item: _Item, turn: JudgeTurn) -> str:
    assert item.question is not None
    return cache_key(turn.config.url, turn.config.model, item.state, item.question)


def _batches(pending: Sequence[_Item]) -> list[list[_Item]]:
    by_state: dict[str, list[_Item]] = {}
    for item in pending:
        by_state.setdefault(item.state, []).append(item)
    batches: list[list[_Item]] = []
    for state, group in by_state.items():
        current: list[_Item] = []
        size = len(state.encode())
        for item in group:
            assert item.question is not None
            cost = len(item.question.canonical().encode()) + 64
            if current and (len(current) >= MAX_QUESTIONS_PER_REQUEST or size + cost > MAX_REQUEST_BYTES):
                batches.append(current)
                current, size = [], len(state.encode())
            current.append(item)
            size += cost
        batches.append(current)
    return batches


async def judge_pass(turn: JudgeTurn, *, abort: AbortToken | None = None) -> JudgeResult:
    running, skipped = _eligible(turn)
    over = _over_budget(running)
    items = [_prepare(candidate, turn, over_budget=candidate.definition.fragment_id in over) for candidate in running]
    replayed: dict[str, dict[str, Any]] = {}
    pending: list[_Item] = []
    for item in items:
        fid = item.definition.fragment_id
        if item.skip_reason:
            continue
        stored = matching_replay(
            turn.replay_records,
            fragment_id=fid,
            raw_fingerprint=item.raw_fingerprint,
            policy_fingerprint=item.policy_fingerprint,
        )
        if stored is not None:
            if item.definition.resolution not in DRAWN_RESOLUTIONS:
                replayed[fid] = _replayed(item, stored)
                continue
            if _redraw(item, stored):
                continue
        if invalidated_anchor(turn.replay_records, fid):
            item.replay_invalidated = SkipReason.MISSING_ANCHOR
        if (hit := RAW_ANSWER_CACHE.get(_cache_key(item, turn))) is not None:
            item.answer, item.answer_source, item.returned_model = hit.answer, "cache", hit.returned_model
        else:
            pending.append(item)

    requests = await _issue(pending, turn, abort=abort) if pending else 0
    evaluations: list[dict[str, Any]] = []
    for item in items:
        # A decision that could not answer drops out here and the turn carries on
        # with the ones that did. Nothing is injected on its behalf.
        record = replayed.get(item.definition.fragment_id)
        if record is None and not item.skip_reason:
            record = _resolved_record(item, turn)
        if record is None:
            skipped.append(_skipped_row(item))
        else:
            evaluations.append(record)
    return JudgeResult(
        evaluations=evaluations,
        skipped=skipped,
        cooldowns=cooldown.advance(
            turn.prior_cooldowns,
            [record["fragment_id"] for record in evaluations],
            [candidate.definition for candidate in turn.candidates],
        ),
        guidance=decision_guidance_block(evaluations),
        requests=requests,
    )


async def _issue(pending: Sequence[_Item], turn: JudgeTurn, *, abort: AbortToken | None) -> int:
    config = turn.config
    client = DecisionClient(config.url, config.api_key, config.model, timeout=REQUEST_TIMEOUT_SECONDS, proxy=config.proxy)
    if abort is not None and abort.is_aborted:
        raise DecisionCancelled("stopped during the judge pass")
    batches = _batches(pending)
    sent = batches[:MAX_BATCHES]
    for batch in batches[len(sent) :]:
        for item in batch:
            item.skip_reason = SkipReason.BUDGET_EXHAUSTED
    # No batch reads another's answer, so none waits on another: the judge answers
    # each in about the same time, and the pass costs its slowest request
    # instead of their sum (four states: 2.5 s sequential, 0.7 s together).
    outcomes = await asyncio.gather(
        *(
            client.decide(
                batch[0].state,
                [item.question for item in batch if item.question is not None],
                timeout=REQUEST_TIMEOUT_SECONDS,
                abort=abort,
            )
            for batch in sent
        ),
        return_exceptions=True,
    )
    if cancelled := next((outcome for outcome in outcomes if isinstance(outcome, DecisionCancelled)), None):
        raise cancelled
    for batch, outcome in zip(sent, outcomes, strict=True):
        if isinstance(outcome, httpx.TimeoutException):
            reason = SkipReason.TIMEOUT
        elif isinstance(outcome, (LLMCallError, DecisionTransportError, httpx.HTTPError)):
            logger.warning("Decision batch of %d failed (%r); skipping those decisions", len(batch), outcome)
            reason = SkipReason.TRANSPORT_FAILURE
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            _apply(batch, outcome, turn)
            continue
        for item in batch:
            item.skip_reason = reason
    return len(sent)


def _apply(batch: Sequence[_Item], response: DecisionResponse, turn: JudgeTurn) -> None:
    for item in batch:
        item.elapsed_ms = response.elapsed_ms
        answer = response.answers.get(item.definition.fragment_id)
        if answer is None:
            item.skip_reason = SkipReason.INVALID_ANSWER
            continue
        item.answer, item.answer_source, item.returned_model = answer, "live", response.returned_model
        # A gated answer is not cached, so the next turn asks again rather than
        # replaying a verdict the author said was too unsure to use.
        if not _gated(item.definition, answer):
            RAW_ANSWER_CACHE.put(_cache_key(item, turn), CachedAnswer(answer, response.returned_model))
