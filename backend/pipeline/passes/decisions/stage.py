from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

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
    DecisionCancelled,
    DecisionClient,
    DecisionTransportError,
    LLMCallError,
    NoulQuestion,
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
    FallbackReason,
    SkipReason,
    draw_uniform,
    resolve_roll,
    resolve_threshold,
)

logger = logging.getLogger(__name__)

MAX_DECISIONS_PER_EXCHANGE = 32
MAX_DECISIONS_PER_CARD = 8
MAX_REQUEST_ATTEMPTS = 4
REQUEST_TIMEOUT_SECONDS = 3.0
STAGE_BUDGET_SECONDS = 6.0

_SITUATION_MACROS = frozenset({"last_message", "last_assistant_message", "recent_history"})

_GLOBAL_SOURCE = "global"

#: The evaluation fields that ride the turn stream (see ``as_event_data``). Every
#: unbounded one -- rendered state, question, criteria, both authored outputs --
#: is deliberately absent; so are the fingerprints and usage, which only the
#: Inspector reads.
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
    "fallback_reason",
    "replay_invalidated",
    "probability",
    "draw",
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
    """A ``decision`` row that could not be parsed into a definition.

    Carried to the stage rather than dropped where it is read. The authoring API
    rejects these on write, so a row that reaches here arrived another way -- a
    preset import, or a definition a later schema invalidated -- and those are
    exactly the cases where an author needs to be told rather than left wondering
    why their question never fires.
    """

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
        """The stage result as the turn stream publishes it.

        A projection rather than the stored record: a rendered state is up to
        16 KiB, and nothing watching a turn go by needs the classifier's input --
        the Inspector fetches the full record from the reply it is stored on.
        What stays is what identifies the decision and says how it resolved.
        """
        return {
            "evaluations": [{key: row[key] for key in _EVENT_FIELDS if key in row} for row in self.evaluations],
            "skipped": self.skipped,
            "cooldowns": self.cooldowns,
        }

    def as_envelope(self) -> dict[str, Any]:
        return envelope(self.evaluations, self.skipped)


@dataclass(slots=True)
class _Prepared:
    candidate: DecisionCandidate
    state: str = ""
    instructions: str = ""
    criteria: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    raw_fingerprint: str = ""
    policy_fingerprint: str = ""
    fallback_reason: str = ""
    oversize: tuple[int, int] = (0, 0)
    replay_invalidated: str = ""

    @property
    def definition(self) -> DecisionDefinition:
        return self.candidate.definition

    def question(self) -> NoulQuestion:
        return NoulQuestion(
            key=self.definition.fragment_id,
            instructions=self.instructions,
            criteria=self.criteria,
            question_type=self.definition.decision_type,
        )


def _resolve_text(text: str, snapshot: DecisionSnapshot) -> str:
    return render(text, snapshot, allowed=TEXT_MACROS)


def _eligible(turn: DecisionsTurn) -> tuple[list[DecisionCandidate], list[dict[str, Any]]]:
    running: list[DecisionCandidate] = []
    skipped: list[dict[str, Any]] = []
    resting = cooldown.blocked(turn.prior_cooldowns)

    def _skip(candidate: DecisionCandidate, reason: str) -> None:
        skipped.append(
            {
                "fragment_id": candidate.definition.fragment_id,
                "fragment_label": candidate.definition.label,
                "source": candidate.source,
                "reason": reason,
            }
        )

    for broken in turn.invalid:
        skipped.append(
            {
                "fragment_id": broken.fragment_id,
                "fragment_label": broken.label,
                "source": broken.source,
                "reason": SkipReason.INVALID_DEFINITION,
            }
        )

    for candidate in turn.candidates:
        definition = candidate.definition
        if candidate.card_id and candidate.card_id not in turn.approved_cards:
            _skip(candidate, SkipReason.NOT_APPROVED)
            continue
        if definition.fragment_id in resting:
            _skip(candidate, SkipReason.RESTING)
            continue
        running.append(candidate)
    return running, skipped


def _over_budget(running: Sequence[DecisionCandidate]) -> set[str]:
    over: set[str] = set()
    per_card: dict[str, int] = {}
    for index, candidate in enumerate(running):
        if index >= MAX_DECISIONS_PER_EXCHANGE:
            over.add(candidate.definition.fragment_id)
            continue
        if candidate.card_id is None:
            continue
        taken = per_card.get(candidate.card_id, 0)
        if taken >= MAX_DECISIONS_PER_CARD:
            over.add(candidate.definition.fragment_id)
        else:
            per_card[candidate.card_id] = taken + 1
    return over


def _prepare(candidate: DecisionCandidate, turn: DecisionsTurn, *, over_budget: bool) -> _Prepared:
    definition = candidate.definition
    prepared = _Prepared(candidate=candidate)
    snapshot = turn.snapshot
    prepared.outputs = {key: _resolve_text(value, snapshot) for key, value in definition.outputs.items()}
    prepared.policy_fingerprint = resolution_policy_fingerprint(definition, scope=turn.scope)

    if over_budget:
        prepared.fallback_reason = FallbackReason.BUDGET_EXHAUSTED
        return prepared
    if not turn.config.configured:
        prepared.fallback_reason = FallbackReason.NOT_CONFIGURED
        return prepared
    if template_errors(definition.state_template, allowed=STATE_MACROS):
        prepared.fallback_reason = FallbackReason.INVALID_DEFINITION
        return prepared

    used = set(macros_used(definition.state_template))
    try:
        prepared.state = render(definition.state_template, snapshot, allowed=STATE_MACROS)
        prepared.instructions = _resolve_text(definition.instructions, snapshot)
        prepared.criteria = {key: _resolve_text(value, snapshot) for key, value in definition.criteria.items()}
    except UnavailableMacro as unavailable:
        logger.info(
            "Decision %s: {{%s}} is unavailable in %s scope; using the authored fallback",
            definition.fragment_id,
            unavailable.macro,
            turn.scope,
        )
        prepared.fallback_reason = FallbackReason.UNAVAILABLE_CONTEXT
        return prepared

    if used and used <= _SITUATION_MACROS and not any((snapshot.value(macro) or "").strip() for macro in used):
        prepared.fallback_reason = FallbackReason.EMPTY_INPUT
        return prepared
    if oversized_state(prepared.state) or oversized_question(prepared.instructions, prepared.criteria):
        prepared.oversize = (
            len(prepared.state.encode("utf-8")),
            len(prepared.instructions.encode("utf-8")) + sum(len(text.encode("utf-8")) for text in prepared.criteria.values()),
        )
        prepared.state = ""
        prepared.instructions = ""
        prepared.criteria = {}
        prepared.fallback_reason = FallbackReason.OVERSIZED_INPUT
        return prepared

    prepared.raw_fingerprint = raw_request_fingerprint(
        model=turn.config.model,
        state=prepared.state,
        instructions=prepared.instructions,
        criteria=prepared.criteria,
        question_type=definition.decision_type,
    )
    return prepared


def _record(
    prepared: _Prepared,
    turn: DecisionsTurn,
    *,
    outcome: str,
    answer_source: str,
    probability: float | None = None,
    draw: float | None = None,
    returned_model: str = "",
    fallback_reason: str = "",
    request_id: str = "",
    elapsed_ms: int = 0,
    usage: Mapping[str, Any] | None = None,
    usage_owner: bool = False,
    occurrence_id: str | None = None,
) -> dict[str, Any]:
    definition = prepared.definition
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
        "rendered_instructions": prepared.instructions,
        "rendered_criteria": dict(prepared.criteria),
        "raw_request_fingerprint": prepared.raw_fingerprint,
        "resolution_policy_fingerprint": prepared.policy_fingerprint,
        "outputs": dict(prepared.outputs),
        "requested_model": turn.config.model,
        "returned_model": returned_model,
        "outcome": outcome,
        "guidance": prepared.outputs.get(outcome, ""),
        "answer_source": answer_source,
        "elapsed_ms": elapsed_ms,
    }
    if prepared.oversize != (0, 0):
        record["oversize_state_bytes"], record["oversize_question_bytes"] = prepared.oversize
        record["state_limit"] = MAX_STATE_BYTES
        record["question_limit"] = MAX_QUESTION_BYTES
    if prepared.replay_invalidated:
        record["replay_invalidated"] = prepared.replay_invalidated
    if probability is not None:
        record["probability"] = probability
    if draw is not None:
        record["draw"] = draw
    if fallback_reason:
        record["fallback_reason"] = fallback_reason
    if request_id:
        record["request_id"] = request_id
    if usage:
        record["usage"] = dict(usage)
        record["usage_owner"] = 1 if usage_owner else 0
    return record


def _fallback(prepared: _Prepared, turn: DecisionsTurn, reason: str) -> dict[str, Any]:
    return _record(
        prepared, turn, outcome=prepared.definition.default_outcome, answer_source="fallback", fallback_reason=reason
    )


def _resolved(
    prepared: _Prepared,
    turn: DecisionsTurn,
    probability: float,
    *,
    answer_source: str,
    returned_model: str,
    request_id: str = "",
    elapsed_ms: int = 0,
    usage: Mapping[str, Any] | None = None,
    usage_owner: bool = False,
) -> dict[str, Any]:
    definition = prepared.definition
    if definition.resolution == "roll":
        draw = draw_uniform()
        outcome = resolve_roll(probability, draw)
    else:
        draw = None
        outcome = resolve_threshold(probability, definition.threshold if definition.threshold is not None else 0.5)
    return _record(
        prepared,
        turn,
        outcome=outcome,
        answer_source=answer_source,
        probability=probability,
        draw=draw,
        returned_model=returned_model,
        request_id=request_id,
        elapsed_ms=elapsed_ms,
        usage=usage,
        usage_owner=usage_owner,
    )


def _replayed(prepared: _Prepared, turn: DecisionsTurn, stored: Mapping[str, Any]) -> dict[str, Any]:
    record = _record(
        prepared,
        turn,
        outcome=str(stored.get("outcome") or prepared.definition.default_outcome),
        answer_source="replay",
        probability=stored.get("probability"),
        draw=stored.get("draw"),
        returned_model=str(stored.get("returned_model") or ""),
        fallback_reason=str(stored.get("fallback_reason") or ""),
        occurrence_id=str(stored.get("occurrence_id") or "") or None,
    )
    record["replayed_from"] = str(stored.get("answer_source") or "")
    return record


def _batches(pending: Sequence[_Prepared]) -> list[list[_Prepared]]:
    by_state: dict[str, list[_Prepared]] = {}
    for prepared in pending:
        by_state.setdefault(prepared.state, []).append(prepared)

    batches: list[list[_Prepared]] = []
    for group in by_state.values():
        current: list[_Prepared] = []
        for prepared in group:
            probe = [*current, prepared]
            too_many = len(probe) > MAX_QUESTIONS_PER_REQUEST
            too_big = _batch_bytes(probe) > MAX_REQUEST_BYTES
            if current and (too_many or too_big):
                batches.append(current)
                current = [prepared]
            else:
                current = probe
        if current:
            batches.append(current)
    return batches


def _batch_bytes(batch: Sequence[_Prepared]) -> int:
    state = len(batch[0].state.encode("utf-8")) if batch else 0
    return state + sum(prepared.question().rendered_bytes() for prepared in batch) + 64 * len(batch)


def stage_has_work(turn: DecisionsTurn) -> bool:
    """Whether this turn has anything to publish -- a decision to run, or one to
    report as unrunnable. An unparseable definition counts: saying nothing is how
    it went unnoticed."""
    return bool(turn.candidates or turn.invalid)


async def run_decisions(turn: DecisionsTurn, *, abort: AbortToken | None = None) -> DecisionsResult:
    started = time.monotonic()
    running, skipped = _eligible(turn)
    over_budget = _over_budget(running)
    prepared = [_prepare(candidate, turn, over_budget=candidate.definition.fragment_id in over_budget) for candidate in running]

    records: dict[str, dict[str, Any]] = {}
    pending: list[_Prepared] = []
    for item in prepared:
        if item.fallback_reason:
            records[item.definition.fragment_id] = _fallback(item, turn, item.fallback_reason)
            continue
        stored = matching_replay(
            turn.replay_records,
            fragment_id=item.definition.fragment_id,
            raw_fingerprint=item.raw_fingerprint,
            policy_fingerprint=item.policy_fingerprint,
        )
        if stored is not None:
            records[item.definition.fragment_id] = _replayed(item, turn, stored)
            continue
        if invalidated_anchor(turn.replay_records, item.definition.fragment_id):
            item.replay_invalidated = FallbackReason.MISSING_ANCHOR
        pending.append(item)

    cached_namespace = turn.config.namespace
    still_pending: list[_Prepared] = []
    for item in pending:
        key = cache_key(cached_namespace, turn.config.model, item.state, item.question())
        hit = RAW_ANSWER_CACHE.get(key)
        if hit is None:
            still_pending.append(item)
            continue
        records[item.definition.fragment_id] = _resolved(
            item, turn, hit.probability, answer_source="cache", returned_model=hit.returned_model
        )

    requests = 0
    if still_pending:
        requests = await _issue(still_pending, turn, records, started=started, abort=abort)

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
    pending: Sequence[_Prepared],
    turn: DecisionsTurn,
    records: dict[str, dict[str, Any]],
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
    namespace = turn.config.namespace
    issued = 0
    for batch in _batches(pending):
        if abort is not None and abort.is_aborted:
            raise DecisionCancelled("stopped during the decision stage")
        remaining = STAGE_BUDGET_SECONDS - (time.monotonic() - started)
        if issued >= MAX_REQUEST_ATTEMPTS or remaining <= 0:
            for item in batch:
                records.setdefault(item.definition.fragment_id, _fallback(item, turn, FallbackReason.BUDGET_EXHAUSTED))
            continue
        issued += 1
        try:
            response = await client.decide(
                batch[0].state,
                [item.question() for item in batch],
                timeout=min(REQUEST_TIMEOUT_SECONDS, remaining),
                abort=abort,
            )
        except DecisionCancelled:
            raise
        except httpx.TimeoutException:
            logger.warning("Decision batch of %d timed out; using authored fallbacks", len(batch))
            for item in batch:
                records.setdefault(item.definition.fragment_id, _fallback(item, turn, FallbackReason.TIMEOUT))
            continue
        except (LLMCallError, DecisionTransportError, httpx.HTTPError) as error:
            logger.warning("Decision batch of %d failed (%r); using authored fallbacks", len(batch), error)
            for item in batch:
                records.setdefault(item.definition.fragment_id, _fallback(item, turn, FallbackReason.TRANSPORT_FAILURE))
            continue

        first = True
        for item in batch:
            probability = response.answers.get(item.definition.fragment_id)
            if probability is None:
                records.setdefault(item.definition.fragment_id, _fallback(item, turn, FallbackReason.INVALID_ANSWER))
                continue
            RAW_ANSWER_CACHE.put(
                cache_key(namespace, turn.config.model, item.state, item.question()),
                CachedAnswer(probability=probability, returned_model=response.returned_model),
            )
            records[item.definition.fragment_id] = _resolved(
                item,
                turn,
                probability,
                answer_source="live",
                returned_model=response.returned_model,
                request_id=response.request_id,
                elapsed_ms=response.elapsed_ms,
                usage=response.usage,
                usage_owner=first,
            )
            first = False
    return issued
