"""The before-Director decision stage: eligibility, batching, budgets, outcomes.

The stage runs once per exchange, after context, branch history, persona and
fragments are resolved and before the Director's first request. Its shape is
fixed by three invariants:

1. **One frozen snapshot per stage.** Every decision reads the same
   :class:`~.render.DecisionSnapshot`, so no decision can observe a sibling's
   result. That is why the snapshot is built by the caller and handed in whole.
2. **All-or-nothing visibility.** Nothing the stage produces is published until
   it finishes, whatever order the requests completed in.
3. **A failure is always the author's fallback, never a guess.** Missing
   configuration, unavailable context, an oversized input, an exhausted budget, a
   transport failure, a timeout, and an invalid answer all land on the outcome
   the author chose -- with a reason recorded and no fabricated probability.

Budgets are counted in requests *after* grouping and packing, because grouping is
what makes a batch cheap and counting before it would price the feature as if it
had never been designed.
"""

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

# ── Server-side budgets, to be validated during the prototype ────────────────
#: Eligible decisions per exchange, and the share any one card may take of them.
MAX_DECISIONS_PER_EXCHANGE = 32
MAX_DECISIONS_PER_CARD = 8
#: Outbound requests per exchange, counted after grouping and packing.
MAX_REQUEST_ATTEMPTS = 4
#: Seconds per request, and for the whole stage. Two sequential timeouts can
#: consume the entire stage budget; observed successful calls do not establish a
#: worst case, so the stage budget is the one that actually bounds a turn.
REQUEST_TIMEOUT_SECONDS = 3.0
STAGE_BUDGET_SECONDS = 6.0

#: The macros that supply scene content. When a template uses only these and all
#: of them render empty, there is no situation to classify.
_SITUATION_MACROS = frozenset({"last_message", "last_assistant_message", "recent_history"})

_GLOBAL_SOURCE = "global"


@dataclass(frozen=True, slots=True)
class DecisionConfig:
    """The resolved classifier configuration for one turn.

    Credentials come from an existing endpoint row; ``endpoint_identity`` is the
    host and row id, never the key, because it is part of a cache namespace that
    must not carry a secret.
    """

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
    """One enabled decision definition and where it came from."""

    definition: DecisionDefinition
    #: ``global`` or the contributing card id. Card decisions need approval.
    card_id: str | None = None

    @property
    def source(self) -> str:
        return f"card:{self.card_id}" if self.card_id else _GLOBAL_SOURCE


@dataclass(frozen=True, slots=True)
class DecisionsTurn:
    """Everything the stage may read, frozen before it runs.

    The stage has no other inputs on purpose: no ``PipelineContext``, no
    database, no filesystem. What is not here is not available to a decision.
    """

    snapshot: DecisionSnapshot
    candidates: tuple[DecisionCandidate, ...] = ()
    config: DecisionConfig = field(default_factory=DecisionConfig)
    prior_cooldowns: Mapping[str, int] = field(default_factory=dict)
    #: The regeneration target's own evaluation records, if this is a regenerate.
    replay_records: tuple[Mapping[str, Any], ...] = ()
    #: Cards whose decision definitions this machine has approved, as they stand.
    approved_cards: frozenset[str] = frozenset()

    @property
    def scope(self) -> str:
        return self.snapshot.scope


@dataclass(slots=True)
class DecisionsResult:
    """What the stage publishes, all at once, when it is done."""

    evaluations: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    cooldowns: dict[str, int] = field(default_factory=dict)
    guidance: str = ""
    latency_ms: int = 0
    requests: int = 0

    def as_event_data(self) -> dict[str, Any]:
        """The Inspector payload. Probabilities and draws belong here, not in a prompt."""
        return {"evaluations": self.evaluations, "skipped": self.skipped, "cooldowns": self.cooldowns}

    def as_envelope(self) -> dict[str, Any]:
        return envelope(self.evaluations, self.skipped)


# ── Preparation: one rendered, fingerprinted, budget-checked candidate ────────


@dataclass(slots=True)
class _Prepared:
    """A candidate carried through the stage with everything it needs."""

    candidate: DecisionCandidate
    state: str = ""
    instructions: str = ""
    criteria: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    raw_fingerprint: str = ""
    policy_fingerprint: str = ""
    #: Set when the decision cannot be asked; it resolves to its fallback.
    fallback_reason: str = ""
    #: Recorded for an oversized input, whose text is deliberately not kept.
    oversize: tuple[int, int] = (0, 0)
    #: Why a stored record for this fragment was not replayed, when one existed.
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
    """Resolve the three identity macros allowed in authored decision text.

    Resolved before fingerprinting and before injection, so the cache key and
    the Inspector both show what the classifier actually read. Random, clock and
    date macros are not supported here: the first would fragment the cache, and
    the second two would make an unchanged question look different every minute.
    """
    return render(text, snapshot, allowed=TEXT_MACROS)


def _eligible(turn: DecisionsTurn) -> tuple[list[DecisionCandidate], list[dict[str, Any]]]:
    """Split candidates into those that run this exchange and those skipped.

    Deterministic order decides who fits: candidates arrive in fragment order --
    the existing global/card merge with its offset keeping card fragments after
    globals -- so reordering decisions changes budget priority, and nothing else
    does.
    """
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

    for candidate in turn.candidates:
        definition = candidate.definition
        if candidate.card_id and candidate.card_id not in turn.approved_cards:
            # Checked before the response cache and before replay: an
            # unapproved card must not be able to reach a stored answer either,
            # or approval would only gate the first turn.
            _skip(candidate, SkipReason.NOT_APPROVED)
            continue
        if definition.fragment_id in resting:
            _skip(candidate, SkipReason.RESTING)
            continue
        # Budget is not eligibility: an over-budget decision is eligible and
        # records a fallback, which ``_over_budget`` decides on the surviving
        # order. Only the three reasons above mean "did not run at all".
        running.append(candidate)
    return running, skipped


def _over_budget(running: Sequence[DecisionCandidate]) -> set[str]:
    """Which eligible decisions exceed the per-exchange and per-card caps.

    They still appear in the Inspector, with their authored default as the
    outcome: an enabled decision that silently vanished would leave an author
    debugging a scene that never asked its question.
    """
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
    """Render and validate one candidate against the frozen snapshot.

    Independently of its siblings: a template that will not render is this
    decision's problem, and its neighbours still get asked.
    """
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
        # A solo fragment using {{description}} in a group. Never infer an owning
        # or speaking member: the answer would be about a character nobody chose.
        logger.info(
            "Decision %s: {{%s}} is unavailable in %s scope; using the authored fallback",
            definition.fragment_id,
            unavailable.macro,
            turn.scope,
        )
        prepared.fallback_reason = FallbackReason.UNAVAILABLE_CONTEXT
        return prepared

    # The emptiness test is on the macro *values*, not on the rendered text: a
    # template whose only macros are scene content and whose scene content is all
    # empty has nothing to classify, however much literal prose frames it. A
    # custom template that supplies an explicit situation uses some other macro
    # (or none) and is therefore never caught here.
    if used and used <= _SITUATION_MACROS and not any((snapshot.value(macro) or "").strip() for macro in used):
        prepared.fallback_reason = FallbackReason.EMPTY_INPUT
        return prepared
    if oversized_state(prepared.state) or oversized_question(prepared.instructions, prepared.criteria):
        # Never silently truncate what is *sent* -- the dropped half is exactly as
        # likely to hold the fact that decides the answer -- so nothing is sent at
        # all. The record keeps the measurements rather than the prose: an author
        # needs the number, and echoing an over-limit state back onto every reply
        # would store the bytes the limit exists to avoid.
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


# ── Records ──────────────────────────────────────────────────────────────────


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
    """Build one evaluation record.

    Every record snapshots the guidance actually used, so the Inspector shows the
    words that reached the model on *this* turn rather than whatever the fragment
    says now.
    """
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
        # A fresh occurrence on an otherwise-unchanged reply needs a stated
        # reason, or the Inspector shows a reroll nobody asked for.
        record["replay_invalidated"] = prepared.replay_invalidated
    # Absent rather than null: a fallback has no probability to report, and
    # writing one would invite a reader to average it with real answers.
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
        # One owner per shared request, so a batch's cost is reported once
        # instead of being counted again for every fragment that rode it.
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
    """Resolve a probability into an outcome by the definition's own policy."""
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
    """Reuse the target's own outcome, with this turn's guidance.

    The answer, the draw and the outcome come from the stored record; the
    rendered guidance is resolved fresh. That split is what lets an author edit
    an output and see the change on the next regeneration without another
    classifier call and without a new roll.
    """
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
    # A replayed fallback stays a fallback: pressing regenerate asked for a
    # different reply, not for a retry of the provider that just failed.
    record["replayed_from"] = str(stored.get("answer_source") or "")
    return record


# ── Batching ─────────────────────────────────────────────────────────────────


def _batches(pending: Sequence[_Prepared]) -> list[list[_Prepared]]:
    """Group by identical rendered state, then pack within the request limits.

    Adjacency is neither required nor sufficient: two decisions batch because
    they ask about the same state, not because they sit next to each other in the
    list. Unrelated states are never concatenated to force a batch -- that would
    change the question every one of them is answering.
    """
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
    """An upper bound on one batch's serialized body.

    The shared state is counted once, because it is sent once -- which is the
    whole economic argument for batching in the first place.
    """
    state = len(batch[0].state.encode("utf-8")) if batch else 0
    return state + sum(prepared.question().rendered_bytes() for prepared in batch) + 64 * len(batch)


# ── The stage ────────────────────────────────────────────────────────────────


def stage_has_work(turn: DecisionsTurn) -> bool:
    """Whether this exchange has any decision to evaluate or record."""
    return bool(turn.candidates)


async def run_decisions(turn: DecisionsTurn, *, abort: AbortToken | None = None) -> DecisionsResult:
    """Evaluate every eligible decision and publish the outcomes together.

    Raises :class:`DecisionCancelled` if the user stops. A stop is not a provider
    failure: it must not produce fallback guidance and must not let generation
    continue, so it propagates rather than being folded into a result.
    """
    started = time.monotonic()
    running, skipped = _eligible(turn)
    over_budget = _over_budget(running)
    prepared = [_prepare(candidate, turn, over_budget=candidate.definition.fragment_id in over_budget) for candidate in running]

    # Fragment order throughout, so the published guidance order is the authored
    # order regardless of which batch answered first.
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
        # A cache hit reuses the recorded answer, and roll mode still draws
        # fresh: this is a new occurrence, so it gets its own dice.
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
    # Cooldowns advance for every evaluation -- live, cached, replayed or fallen
    # back -- and age even when nothing was eligible, so disabling every decision
    # cannot freeze a timer.
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
    """Send the grouped batches sequentially and fold their answers in.

    Sequential in the first release, which buys simple cancellation and a budget
    that is actually the sum of what ran. Returns the number of requests issued,
    counted after grouping and packing.
    """
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
                # Capped by the exchange's remaining decision time, so two
                # sequential timeouts cannot exceed the stage budget between them.
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
            # No retry and no split-and-retry in the interactive path: a rate
            # limit or a refused batch must not become unbounded extra attempts
            # inside a turn the user is waiting on.
            logger.warning("Decision batch of %d failed (%r); using authored fallbacks", len(batch), error)
            for item in batch:
                records.setdefault(item.definition.fragment_id, _fallback(item, turn, FallbackReason.TRANSPORT_FAILURE))
            continue

        first = True
        for item in batch:
            probability = response.answers.get(item.definition.fragment_id)
            if probability is None:
                # One invalid sibling is isolated: a partially valid response
                # still supplies its valid answers.
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


__all__ = [
    "MAX_DECISIONS_PER_CARD",
    "MAX_DECISIONS_PER_EXCHANGE",
    "MAX_REQUEST_ATTEMPTS",
    "REQUEST_TIMEOUT_SECONDS",
    "STAGE_BUDGET_SECONDS",
    "DecisionCandidate",
    "DecisionConfig",
    "DecisionsResult",
    "DecisionsTurn",
    "run_decisions",
    "stage_has_work",
]
