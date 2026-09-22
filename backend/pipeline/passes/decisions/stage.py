from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from ....core import DecisionDefinition, DecisionFacet
from ....inference import (
    MAX_QUESTION_BYTES,
    MAX_QUESTIONS_PER_EXCHANGE,
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
    DecisionTransportError,
    LLMCallError,
    ScoreAnswer,
    cache_key,
    cache_namespace,
)
from . import cooldown
from .guidance import decision_guidance_block
from .records import (
    envelope,
    invalidated_anchor,
    matching_replay,
    raw_request_fingerprint,
    resolution_policy_fingerprint,
)
from .render import (
    STATE_MACROS,
    TEXT_MACROS,
    DecisionSnapshot,
    UnavailableMacro,
    macros_used,
    oversized_question,
    oversized_state,
    render,
    template_errors,
)
from .resolve import (
    FAILURE_REASONS,
    SkipReason,
    draw_uniform,
    resolve_argmax,
    resolve_nearest,
    resolve_roll,
    resolve_threshold,
    resolve_weighted,
)

logger = logging.getLogger(__name__)

MAX_DECISIONS_PER_EXCHANGE = 32
MAX_DECISIONS_PER_CARD = 8
MAX_REQUEST_ATTEMPTS = 4
REQUEST_TIMEOUT_SECONDS = 3.0
STAGE_BUDGET_SECONDS = 6.0
_SITUATION_MACROS = frozenset({"last_message", "last_assistant_message", "recent_history"})
_GLOBAL_SOURCE = "global"

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
    "facets",
    "elapsed_ms",
)


@dataclass(frozen=True, slots=True)
class DecisionConfig:
    url: str = ""
    api_key: str = ""
    model: str = ""
    proxy: str = ""
    endpoint_identity: str = ""
    revision: int = 0

    @property
    def configured(self) -> bool:
        return bool(self.url and self.model)

    @property
    def namespace(self) -> str:
        return cache_namespace(endpoint_identity=self.endpoint_identity, config_revision=self.revision)


@dataclass(frozen=True, slots=True)
class DecisionCandidate:
    definition: DecisionDefinition
    card_id: str | None = None

    @property
    def source(self) -> str:
        return f"card:{self.card_id}" if self.card_id else _GLOBAL_SOURCE


@dataclass(frozen=True, slots=True)
class InvalidDecision:
    fragment_id: str
    label: str = ""
    card_id: str | None = None

    @property
    def source(self) -> str:
        return f"card:{self.card_id}" if self.card_id else _GLOBAL_SOURCE


@dataclass(frozen=True, slots=True)
class DecisionsTurn:
    snapshot: DecisionSnapshot
    candidates: tuple[DecisionCandidate, ...] = ()
    config: DecisionConfig = field(default_factory=DecisionConfig)
    prior_cooldowns: Mapping[str, int] = field(default_factory=dict)
    replay_records: tuple[Mapping[str, Any], ...] = ()
    approved_cards: frozenset[str] = frozenset()
    invalid: tuple[InvalidDecision, ...] = ()

    @property
    def scope(self) -> str:
        return self.snapshot.scope


@dataclass(slots=True)
class DecisionsResult:
    evaluations: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    cooldowns: dict[str, int] = field(default_factory=dict)
    guidance: str = ""
    latency_ms: int = 0
    requests: int = 0

    def as_event_data(self) -> dict[str, Any]:
        evaluations: list[dict[str, Any]] = []
        for row in self.evaluations:
            projected = {key: row[key] for key in _EVENT_FIELDS if key in row}
            if "facets" in projected:
                projected["facets"] = [
                    {
                        key: value
                        for key, value in facet.items()
                        if key not in {"rendered_instructions", "rendered_criteria", "outputs"}
                    }
                    for facet in projected["facets"]
                ]
            evaluations.append(projected)
        return {"evaluations": evaluations, "skipped": self.skipped, "cooldowns": self.cooldowns}

    def as_envelope(self) -> dict[str, Any]:
        return envelope(self.evaluations, self.skipped)


Criteria = Mapping[str, str] | tuple[str, ...]
Answer = float | ChoiceAnswer | ScoreAnswer


@dataclass(slots=True)
class _PreparedQuestion:
    owner: _Prepared
    key: str
    instructions: str
    criteria: Criteria
    question_type: str
    facet: DecisionFacet | None = None
    branch: str = ""
    failure: str = ""
    answer: Answer | None = None
    answer_source: str = ""
    returned_model: str = ""
    request_id: str = ""
    elapsed_ms: int = 0
    usage: Mapping[str, Any] = field(default_factory=dict)
    usage_owner: bool = False

    def question(self) -> DecisionQuestion:
        identity = f"{self.facet.key}:{self.branch}" if self.facet else ""
        return DecisionQuestion(self.key, self.instructions, self.criteria, self.question_type, identity)

    @property
    def fingerprint(self) -> str:
        return raw_request_fingerprint(
            model=self.owner.model,
            state=self.owner.state,
            instructions=self.instructions,
            criteria=self.criteria,
            question_type=self.question_type,
            facet_key=self.facet.key if self.facet else "",
            branch_key=self.branch,
        )


@dataclass(slots=True)
class _Prepared:
    candidate: DecisionCandidate
    model: str = ""
    state: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    facet_outputs: dict[str, dict[str, str]] = field(default_factory=dict)
    questions: list[_PreparedQuestion] = field(default_factory=list)
    raw_fingerprint: str = ""
    policy_fingerprint: str = ""
    skip_reason: str = ""
    oversize: tuple[int, int] = (0, 0)
    replay_invalidated: str = ""

    @property
    def definition(self) -> DecisionDefinition:
        return self.candidate.definition

    @property
    def primary(self) -> _PreparedQuestion | None:
        return self.questions[0] if self.questions and self.questions[0].facet is None else None


def _resolve_text(text: str, snapshot: DecisionSnapshot) -> str:
    return render(text, snapshot, allowed=TEXT_MACROS)


def _render_criteria(criteria: Criteria, snapshot: DecisionSnapshot) -> Criteria:
    if isinstance(criteria, Mapping):
        return {key: _resolve_text(value, snapshot) for key, value in criteria.items()}
    return tuple(_resolve_text(value, snapshot) for value in criteria)


def _eligible(turn: DecisionsTurn) -> tuple[list[DecisionCandidate], list[dict[str, Any]]]:
    running: list[DecisionCandidate] = []
    skipped = [
        {
            "fragment_id": broken.fragment_id,
            "fragment_label": broken.label,
            "source": broken.source,
            "reason": SkipReason.INVALID_DEFINITION,
            "failed": 1,
        }
        for broken in turn.invalid
    ]
    resting = cooldown.blocked(turn.prior_cooldowns)
    for candidate in turn.candidates:
        reason = ""
        if candidate.card_id and candidate.card_id not in turn.approved_cards:
            reason = SkipReason.NOT_APPROVED
        elif candidate.definition.fragment_id in resting:
            reason = SkipReason.RESTING
        if reason:
            skipped.append(
                {
                    "fragment_id": candidate.definition.fragment_id,
                    "fragment_label": candidate.definition.label,
                    "source": candidate.source,
                    "reason": reason,
                }
            )
        else:
            running.append(candidate)
    return running, skipped


def _over_budget(running: Sequence[DecisionCandidate]) -> set[str]:
    over: set[str] = set()
    per_card: dict[str, int] = {}
    for index, candidate in enumerate(running):
        if index >= MAX_DECISIONS_PER_EXCHANGE:
            over.add(candidate.definition.fragment_id)
        elif candidate.card_id:
            count = per_card.get(candidate.card_id, 0)
            if count >= MAX_DECISIONS_PER_CARD:
                over.add(candidate.definition.fragment_id)
            else:
                per_card[candidate.card_id] = count + 1
    return over


def _prepare(candidate: DecisionCandidate, turn: DecisionsTurn, *, over_budget: bool) -> _Prepared:
    definition = candidate.definition
    prepared = _Prepared(candidate=candidate, model=turn.config.model)
    prepared.policy_fingerprint = resolution_policy_fingerprint(definition, scope=turn.scope)
    try:
        prepared.outputs = {key: _resolve_text(value, turn.snapshot) for key, value in definition.outputs.items()}
        prepared.facet_outputs = {
            facet.key: {key: _resolve_text(value, turn.snapshot) for key, value in facet.outputs.items()}
            for facet in definition.facets
        }
    except UnavailableMacro:
        prepared.skip_reason = SkipReason.UNAVAILABLE_CONTEXT
        return prepared
    if over_budget:
        prepared.skip_reason = SkipReason.BUDGET_EXHAUSTED
        return prepared
    if not turn.config.configured:
        prepared.skip_reason = SkipReason.NOT_CONFIGURED
        return prepared
    if template_errors(definition.state_template, allowed=STATE_MACROS):
        prepared.skip_reason = SkipReason.INVALID_DEFINITION
        return prepared
    used = set(macros_used(definition.state_template))
    try:
        prepared.state = render(definition.state_template, turn.snapshot, allowed=STATE_MACROS)
        primary = _PreparedQuestion(
            owner=prepared,
            key=definition.fragment_id,
            instructions=_resolve_text(definition.instructions, turn.snapshot),
            criteria=_render_criteria(definition.criteria, turn.snapshot),
            question_type=definition.decision_type,
        )
    except UnavailableMacro:
        prepared.skip_reason = SkipReason.UNAVAILABLE_CONTEXT
        return prepared
    if used and used <= _SITUATION_MACROS and not any((turn.snapshot.value(macro) or "").strip() for macro in used):
        prepared.skip_reason = SkipReason.EMPTY_INPUT
        return prepared
    if oversized_state(prepared.state) or oversized_question(primary.instructions, primary.criteria):
        texts = primary.criteria.values() if isinstance(primary.criteria, Mapping) else primary.criteria
        prepared.oversize = (
            len(prepared.state.encode()),
            len(primary.instructions.encode()) + sum(len(text.encode()) for text in texts),
        )
        prepared.state = ""
        prepared.skip_reason = SkipReason.OVERSIZED_INPUT
        return prepared
    prepared.questions.append(primary)
    for facet_index, facet in enumerate(definition.facets):
        branches: list[tuple[str, str]]
        if isinstance(facet.instructions, str):
            branches = [("", facet.instructions)]
        else:
            branches = [(branch, facet.instructions[branch]) for branch in definition.outcome_keys]
        for branch_index, (branch, instructions) in enumerate(branches):
            question = _PreparedQuestion(
                owner=prepared,
                key=f"{definition.fragment_id}__{facet.key}__{facet_index}_{branch_index}",
                instructions="",
                criteria=(),
                question_type=facet.decision_type,
                facet=facet,
                branch=branch,
            )
            try:
                question.instructions = _resolve_text(instructions, turn.snapshot)
                question.criteria = _render_criteria(facet.criteria, turn.snapshot)
            except UnavailableMacro:
                question.failure = SkipReason.UNAVAILABLE_CONTEXT
            if not question.failure and oversized_question(question.instructions, question.criteria):
                question.failure = SkipReason.OVERSIZED_INPUT
            prepared.questions.append(question)
    fingerprints = [question.fingerprint for question in prepared.questions]
    prepared.raw_fingerprint = hashlib.sha256(json.dumps(fingerprints, separators=(",", ":")).encode()).hexdigest()
    return prepared


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


def _resolve_answer(
    answer: Answer,
    *,
    decision_type: str,
    resolution: str,
    keys: Sequence[str],
    threshold: float | None = None,
) -> tuple[str, float | None]:
    if decision_type == "noul" and isinstance(answer, (int, float)):
        probability = float(answer)
        if resolution == "roll":
            draw = draw_uniform()
            return resolve_roll(probability, draw), draw
        return resolve_threshold(probability, threshold if threshold is not None else 0.5), None
    if isinstance(answer, (ChoiceAnswer, ScoreAnswer)):
        if resolution == "weighted":
            draw = draw_uniform()
            return resolve_weighted(answer.probabilities, keys, draw), draw
        if resolution == "nearest" and isinstance(answer, ScoreAnswer):
            return resolve_nearest(answer.score, keys), None
        return resolve_argmax(answer.probabilities, keys), None
    raise ValueError("answer primitive did not match its definition")


def _question_details(question: _PreparedQuestion) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if question.answer_source:
        details["answer_source"] = question.answer_source
    if question.answer is not None:
        details.update(_answer_fields(question.answer))
    if question.failure:
        details["skip_reason"] = question.failure
    if question.elapsed_ms:
        details["elapsed_ms"] = question.elapsed_ms
    return details


def _is_gated(question: _PreparedQuestion, answer: Answer) -> bool:
    floor = question.facet.confidence_floor if question.facet else question.owner.definition.confidence_floor
    confidence = getattr(answer, "confidence", None)
    return floor is not None and confidence is not None and confidence < floor


def _base_record(prepared: _Prepared, turn: DecisionsTurn, occurrence_id: str | None = None) -> dict[str, Any]:
    definition = prepared.definition
    primary = prepared.primary
    record: dict[str, Any] = {
        "fragment_id": definition.fragment_id,
        "fragment_label": definition.label,
        "injection_label": definition.injection_label,
        "source": prepared.candidate.source,
        "placement": definition.placement,
        "scope": turn.scope,
        "occurrence_id": occurrence_id or str(uuid.uuid4()),
        "input_branch_anchor": turn.snapshot.anchor_message_id,
        "rendered_state": prepared.state,
        "rendered_instructions": primary.instructions if primary else "",
        "rendered_criteria": primary.criteria if primary else {},
        "raw_request_fingerprint": prepared.raw_fingerprint,
        "resolution_policy_fingerprint": prepared.policy_fingerprint,
        "outputs": dict(prepared.outputs),
        "requested_model": turn.config.model,
        "returned_model": primary.returned_model if primary else "",
        "facets": [],
        "discarded_branches": [],
        "elapsed_ms": max((q.elapsed_ms for q in prepared.questions), default=0),
    }
    if prepared.replay_invalidated:
        record["replay_invalidated"] = prepared.replay_invalidated
    usage_question = next((q for q in prepared.questions if q.usage_owner and q.usage), None)
    if usage_question:
        record["usage"] = dict(usage_question.usage)
        record["usage_owner"] = 1
        if usage_question.request_id:
            record["request_id"] = usage_question.request_id
    return record


def _skipped_row(prepared: _Prepared, turn: DecisionsTurn, reason: str) -> dict[str, Any]:
    """The row for a decision that produced nothing.

    Deliberately thin: a skipped decision injected no guidance and resolved to
    no outcome, so there is nothing to record but who it was and why it did not
    run. The oversize numbers are the one exception -- they are the only reason
    an author cannot act on without being told the size they overran.
    """
    definition = prepared.definition
    row: dict[str, Any] = {
        "fragment_id": definition.fragment_id,
        "fragment_label": definition.label,
        "source": prepared.candidate.source,
        "reason": reason,
    }
    # Separates "something broke" from the routine skips an author configured on
    # purpose, so the client can decide what is worth saying out loud without
    # keeping its own copy of this vocabulary.
    if reason in FAILURE_REASONS:
        row["failed"] = 1
    if prepared.oversize != (0, 0):
        row["oversize_state_bytes"], row["oversize_question_bytes"] = prepared.oversize
        row["state_limit"], row["question_limit"] = MAX_STATE_BYTES, MAX_QUESTION_BYTES
    return row


def _resolved_record(prepared: _Prepared, turn: DecisionsTurn) -> dict[str, Any] | None:
    """The evaluation record, or ``None`` with ``prepared.skip_reason`` set.

    Returning nothing is the whole point: an unanswered decision contributes no
    outcome and no guidance, and the caller turns it into a skip row.
    """
    definition = prepared.definition
    primary = prepared.primary
    if primary is None or primary.answer is None:
        prepared.skip_reason = (primary.failure if primary else "") or prepared.skip_reason or SkipReason.INVALID_ANSWER
        return None
    confidence = getattr(primary.answer, "confidence", None)
    if definition.confidence_floor is not None and confidence is not None and confidence < definition.confidence_floor:
        prepared.skip_reason = SkipReason.LOW_CONFIDENCE
        return None
    try:
        outcome, draw = _resolve_answer(
            primary.answer,
            decision_type=definition.decision_type,
            resolution=definition.resolution,
            keys=definition.outcome_keys,
            threshold=definition.threshold,
        )
    except ValueError:
        prepared.skip_reason = SkipReason.INVALID_ANSWER
        return None
    record = _base_record(prepared, turn)
    record.update(
        {
            "outcome": outcome,
            "guidance": prepared.outputs.get(outcome, ""),
            "answer_source": primary.answer_source,
            **_answer_fields(primary.answer),
        }
    )
    if draw is not None:
        record["draw"] = draw

    for facet in definition.facets:
        questions = [q for q in prepared.questions if q.facet is facet]
        selected = next((q for q in questions if not q.branch or q.branch == outcome), None)
        for discarded in questions:
            if discarded is selected or discarded.answer is None:
                continue
            record["discarded_branches"].append(
                {"key": facet.key, "label": facet.label, "branch": discarded.branch, **_question_details(discarded)}
            )
        facet_record: dict[str, Any] = {
            "key": facet.key,
            "label": facet.label,
            "type": facet.decision_type,
            "branch": selected.branch if selected else outcome,
            "rendered_instructions": selected.instructions if selected else "",
            "rendered_criteria": selected.criteria if selected else {},
            "outputs": dict(prepared.facet_outputs.get(facet.key, {})),
        }
        if selected is None or selected.answer is None:
            facet_record.update(
                {
                    "skip_reason": (selected.failure if selected else "") or SkipReason.INVALID_FACET_ANSWER,
                    "guidance": "",
                }
            )
            record["facets"].append(facet_record)
            continue
        facet_record.update(_answer_fields(selected.answer))
        facet_record["answer_source"] = selected.answer_source
        facet_confidence = getattr(selected.answer, "confidence", None)
        if facet.confidence_floor is not None and facet_confidence is not None and facet_confidence < facet.confidence_floor:
            facet_record.update({"skip_reason": SkipReason.LOW_CONFIDENCE, "guidance": ""})
            record["facets"].append(facet_record)
            continue
        try:
            facet_outcome, facet_draw = _resolve_answer(
                selected.answer,
                decision_type=facet.decision_type,
                resolution=facet.resolution,
                keys=facet.outcome_keys,
                threshold=0.5,
            )
        except ValueError:
            facet_record.update({"skip_reason": SkipReason.INVALID_FACET_ANSWER, "guidance": ""})
        else:
            facet_record["outcome"] = facet_outcome
            facet_record["guidance"] = prepared.facet_outputs.get(facet.key, {}).get(facet_outcome, "")
            if facet_draw is not None:
                facet_record["draw"] = facet_draw
        record["facets"].append(facet_record)
    return record


def _replayed(prepared: _Prepared, turn: DecisionsTurn, stored: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(stored)
    record.update(
        {
            "fragment_label": prepared.definition.label,
            "injection_label": prepared.definition.injection_label,
            "source": prepared.candidate.source,
            "outputs": dict(prepared.outputs),
            "answer_source": "replay",
            "replayed_from": str(stored.get("answer_source") or ""),
            "guidance": prepared.outputs.get(str(stored.get("outcome") or ""), ""),
        }
    )
    facets = []
    by_key = {facet.key: facet for facet in prepared.definition.facets}
    for raw in stored.get("facets", []):
        if not isinstance(raw, Mapping) or (facet := by_key.get(str(raw.get("key") or ""))) is None:
            continue
        item = dict(raw)
        item["answer_source"] = "replay"
        item["outputs"] = dict(prepared.facet_outputs.get(facet.key, {}))
        item["guidance"] = item["outputs"].get(str(item.get("outcome") or ""), "")
        facets.append(item)
    record["facets"] = facets
    return record


def _batches(pending: Sequence[_PreparedQuestion]) -> list[list[_PreparedQuestion]]:
    by_state: dict[str, list[_PreparedQuestion]] = {}
    for question in pending:
        by_state.setdefault(question.owner.state, []).append(question)
    batches: list[list[_PreparedQuestion]] = []
    for group in by_state.values():
        current: list[_PreparedQuestion] = []
        for question in group:
            probe = [*current, question]
            size = len(question.owner.state.encode()) + sum(q.question().rendered_bytes() + 64 for q in probe)
            if current and (len(probe) > MAX_QUESTIONS_PER_REQUEST or size > MAX_REQUEST_BYTES):
                batches.append(current)
                current = [question]
            else:
                current = probe
        if current:
            batches.append(current)
    return batches


def stage_has_work(turn: DecisionsTurn) -> bool:
    return bool(turn.candidates or turn.invalid)


async def run_decisions(turn: DecisionsTurn, *, abort: AbortToken | None = None) -> DecisionsResult:
    started = time.monotonic()
    running, skipped = _eligible(turn)
    over = _over_budget(running)
    prepared = [_prepare(candidate, turn, over_budget=candidate.definition.fragment_id in over) for candidate in running]
    records: dict[str, dict[str, Any]] = {}
    # Keyed rather than appended so the skip list stays in candidate order no
    # matter which pass gave up on a decision.
    failed: dict[str, dict[str, Any]] = {}
    pending: list[_PreparedQuestion] = []
    question_budget = MAX_QUESTIONS_PER_EXCHANGE

    for item in prepared:
        fid = item.definition.fragment_id
        if item.skip_reason:
            failed[fid] = _skipped_row(item, turn, item.skip_reason)
            continue
        stored = matching_replay(
            turn.replay_records,
            fragment_id=fid,
            raw_fingerprint=item.raw_fingerprint,
            policy_fingerprint=item.policy_fingerprint,
        )
        if stored is not None:
            records[fid] = _replayed(item, turn, stored)
            continue
        if invalidated_anchor(turn.replay_records, fid):
            item.replay_invalidated = SkipReason.MISSING_ANCHOR
        for question in item.questions:
            if question.failure:
                continue
            if question_budget <= 0:
                question.failure = SkipReason.BUDGET_EXHAUSTED
                continue
            question_budget -= 1
            key = cache_key(turn.config.namespace, turn.config.model, item.state, question.question())
            hit = RAW_ANSWER_CACHE.get(key)
            if hit is not None:
                if _is_gated(question, hit.answer):
                    RAW_ANSWER_CACHE.discard(key)
                    pending.append(question)
                    continue
                question.answer = hit.answer
                question.answer_source = "cache"
                question.returned_model = hit.returned_model
            else:
                pending.append(question)

    requests = await _issue(pending, turn, started=started, abort=abort) if pending else 0
    for item in prepared:
        fid = item.definition.fragment_id
        if fid in records or fid in failed:
            continue
        # A decision that could not answer drops out here and the turn carries on
        # with the ones that did. Nothing is injected on its behalf.
        record = _resolved_record(item, turn)
        if record is None:
            failed[fid] = _skipped_row(item, turn, item.skip_reason)
        else:
            records[fid] = record
    skipped.extend(failed[item.definition.fragment_id] for item in prepared if item.definition.fragment_id in failed)
    result = DecisionsResult(
        evaluations=[records[item.definition.fragment_id] for item in prepared if item.definition.fragment_id in records],
        skipped=skipped,
        latency_ms=int((time.monotonic() - started) * 1000),
        requests=requests,
    )
    result.guidance = decision_guidance_block(result.evaluations)
    result.cooldowns = cooldown.advance(
        turn.prior_cooldowns,
        [record["fragment_id"] for record in result.evaluations],
        [candidate.definition for candidate in turn.candidates],
    )
    return result


async def _issue(
    pending: Sequence[_PreparedQuestion],
    turn: DecisionsTurn,
    *,
    started: float,
    abort: AbortToken | None,
) -> int:
    client = DecisionClient(
        turn.config.url,
        api_key=turn.config.api_key,
        model=turn.config.model,
        timeout=REQUEST_TIMEOUT_SECONDS,
        proxy=turn.config.proxy,
    )
    issued = 0
    for batch in _batches(pending):
        remaining = STAGE_BUDGET_SECONDS - (time.monotonic() - started)
        if abort is not None and abort.is_aborted:
            raise DecisionCancelled("stopped during the decision stage")
        if issued >= MAX_REQUEST_ATTEMPTS or remaining <= 0:
            for question in batch:
                question.failure = SkipReason.BUDGET_EXHAUSTED
            continue
        issued += 1
        try:
            response = await client.decide(
                batch[0].owner.state,
                [question.question() for question in batch],
                timeout=min(REQUEST_TIMEOUT_SECONDS, remaining),
                abort=abort,
            )
        except DecisionCancelled:
            raise
        except httpx.TimeoutException:
            for question in batch:
                question.failure = SkipReason.TIMEOUT
            continue
        except (LLMCallError, DecisionTransportError, httpx.HTTPError) as error:
            logger.warning("Decision batch of %d failed (%r); skipping those decisions", len(batch), error)
            for question in batch:
                question.failure = SkipReason.TRANSPORT_FAILURE
            continue
        for index, question in enumerate(batch):
            question.elapsed_ms = response.elapsed_ms
            question.request_id = response.request_id
            question.usage = response.usage
            question.usage_owner = index == 0
            answer = response.answers.get(question.key)
            if answer is None:
                question.failure = SkipReason.INVALID_ANSWER if question.facet is None else SkipReason.INVALID_FACET_ANSWER
                continue
            question.answer = answer
            question.answer_source = "live"
            question.returned_model = response.returned_model
            if not _is_gated(question, answer):
                RAW_ANSWER_CACHE.put(
                    cache_key(turn.config.namespace, turn.config.model, question.owner.state, question.question()),
                    CachedAnswer(answer, response.returned_model),
                )
    return issued
