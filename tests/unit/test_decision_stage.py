"""The decision stage end to end, against a stubbed gateway.

Every path a decision can take is here: replayed, served from cache, asked live,
or fallen back — and every reason a fallback fires. The gateway is stubbed at
the transport boundary (``DecisionClient.decide``) so the adapter's own strict
normalization is exercised separately in ``test_jev_adapter.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

import httpx
import pytest

from backend.core import DEFAULT_STATE_TEMPLATE, parse_decision_definition
from backend.inference import (
    RAW_ANSWER_CACHE,
    AbortToken,
    DecisionCancelled,
    DecisionResponse,
    DecisionTransportError,
    LLMCallError,
)
from backend.pipeline.passes.decisions import (
    MAX_DECISIONS_PER_CARD,
    MAX_DECISIONS_PER_EXCHANGE,
    DecisionCandidate,
    DecisionConfig,
    DecisionSnapshot,
    DecisionsTurn,
    FallbackReason,
    SkipReason,
    envelope,
    run_decisions,
    stage_has_work,
)
from backend.pipeline.passes.decisions import stage as stage_module

CONFIG = DecisionConfig(
    url="https://example.test/api/alpha/decisions",
    api_key="secret",
    model="typesafe/jev-1.13",
    endpoint_identity="example.test#1",
    revision=1,
)

SNAPSHOT = DecisionSnapshot(
    last_message="I shove the door.",
    last_assistant_message="She braces against the frame.",
    user="Tester",
    char="Maren",
    description="A wizard.",
    scope="solo",
    anchor_message_id=42,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    RAW_ANSWER_CACHE.clear()
    yield
    RAW_ANSWER_CACHE.clear()


def _definition(fragment_id: str = "outcome", **overrides):
    row = {
        "id": fragment_id,
        "label": fragment_id.title(),
        "injection_label": fragment_id.title(),
        "field_type": "decision",
        "cooldown_turns": overrides.pop("cooldown_turns", 0),
        "decision_type": "noul",
        "decision_placement": "before_director",
        "decision_state_template": DEFAULT_STATE_TEMPLATE,
        "decision_instructions": f"Does {fragment_id} happen?",
        "decision_criteria": {"true": "It does.", "false": "It does not."},
        "decision_outputs": {"true": f"{fragment_id} succeeded.", "false": f"{fragment_id} failed."},
        "decision_default": "false",
        "decision_resolution": "threshold",
        "decision_threshold": 0.5,
    }
    row.update(overrides)
    definition = parse_decision_definition(row)
    assert definition is not None, row
    return definition


def _candidate(fragment_id: str = "outcome", card_id: str | None = None, **overrides) -> DecisionCandidate:
    return DecisionCandidate(definition=_definition(fragment_id, **overrides), card_id=card_id)


def _turn(*candidates: DecisionCandidate, **overrides) -> DecisionsTurn:
    options = {
        "snapshot": SNAPSHOT,
        "candidates": tuple(candidates),
        "config": CONFIG,
        "prior_cooldowns": {},
        "replay_records": (),
        "approved_cards": frozenset(),
    }
    options.update(overrides)
    return DecisionsTurn(**options)


@dataclass
class FakeGateway:
    """Stands in for the gateway, recording every batch it was asked."""

    answers: dict[str, float] = field(default_factory=dict)
    error: Exception | None = None
    returned_model: str = "typesafe/jev-1.13.2"
    usage: dict = field(default_factory=lambda: {"total_tokens": 11})
    batches: list[list[str]] = field(default_factory=list)
    states: list[str] = field(default_factory=list)

    def install(self, monkeypatch):
        gateway = self

        async def _decide(self, state, questions, *, timeout=None, abort=None):  # noqa: ANN001
            gateway.batches.append([question.key for question in questions])
            gateway.states.append(state)
            if gateway.error is not None:
                raise gateway.error
            return DecisionResponse(
                answers={q.key: gateway.answers[q.key] for q in questions if q.key in gateway.answers},
                invalid=tuple(q.key for q in questions if q.key not in gateway.answers),
                returned_model=gateway.returned_model,
                usage=dict(gateway.usage),
                request_id="req-1",
                elapsed_ms=7,
            )

        monkeypatch.setattr(stage_module.DecisionClient, "decide", _decide)
        return gateway


def _by_id(result):
    return {record["fragment_id"]: record for record in result.evaluations}


# ── the happy path ───────────────────────────────────────────────────────────


async def test_a_live_answer_resolves_injects_and_records(monkeypatch):
    FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    result = await run_decisions(_turn(_candidate()))

    record = _by_id(result)["outcome"]
    assert record["answer_source"] == "live"
    assert record["probability"] == 0.9
    assert record["outcome"] == "true"
    assert record["guidance"] == "outcome succeeded."
    assert record["returned_model"] == "typesafe/jev-1.13.2"
    assert record["input_branch_anchor"] == 42
    assert "draw" not in record  # threshold mode has no draw to report
    assert result.guidance == "**Resolved Decisions**\nOutcome: outcome succeeded."
    assert result.requests == 1


async def test_an_empty_selected_output_suppresses_injection_but_still_records(monkeypatch):
    FakeGateway(answers={"outcome": 0.1}).install(monkeypatch)
    result = await run_decisions(_turn(_candidate(decision_outputs={"true": "held", "false": ""})))

    assert _by_id(result)["outcome"]["outcome"] == "false"
    assert result.guidance == ""
    assert result.evaluations  # invisible to the model, visible to the Inspector


async def test_publication_order_is_fragment_order(monkeypatch):
    FakeGateway(answers={"a": 0.9, "b": 0.9, "c": 0.9}).install(monkeypatch)
    result = await run_decisions(_turn(_candidate("a"), _candidate("b"), _candidate("c")))
    assert [record["fragment_id"] for record in result.evaluations] == ["a", "b", "c"]


# ── batching ─────────────────────────────────────────────────────────────────


async def test_identical_states_share_one_request(monkeypatch):
    gateway = FakeGateway(answers={"a": 0.9, "b": 0.2}).install(monkeypatch)
    result = await run_decisions(_turn(_candidate("a"), _candidate("b")))

    assert gateway.batches == [["a", "b"]]
    assert result.requests == 1
    assert _by_id(result)["a"]["outcome"] == "true"
    assert _by_id(result)["b"]["outcome"] == "false"


async def test_different_states_split_and_are_never_concatenated(monkeypatch):
    gateway = FakeGateway(answers={"a": 0.9, "b": 0.9}).install(monkeypatch)
    await run_decisions(
        _turn(
            _candidate("a"),
            _candidate("b", decision_state_template="Only the request: {{last_message}}"),
        )
    )
    assert sorted(sorted(batch) for batch in gateway.batches) == [["a"], ["b"]]
    assert len(set(gateway.states)) == 2
    # Unrelated states must never be welded together to force a batch.
    assert all("Previous reply" not in state or "Only the request" not in state for state in gateway.states)


async def test_a_batch_is_packed_within_the_question_limit(monkeypatch):
    ids = [f"q{i}" for i in range(stage_module.MAX_QUESTIONS_PER_REQUEST + 3)]
    gateway = FakeGateway(answers=dict.fromkeys(ids, 0.9)).install(monkeypatch)
    await run_decisions(_turn(*(_candidate(fragment_id) for fragment_id in ids)))

    assert len(gateway.batches) == 2
    assert all(len(batch) <= stage_module.MAX_QUESTIONS_PER_REQUEST for batch in gateway.batches)
    assert sorted(key for batch in gateway.batches for key in batch) == sorted(ids)


async def test_one_invalid_sibling_answer_is_isolated(monkeypatch):
    # A partially valid response still supplies its valid answers.
    FakeGateway(answers={"good": 0.9}).install(monkeypatch)
    result = await run_decisions(_turn(_candidate("good"), _candidate("bad")))

    records = _by_id(result)
    assert records["good"]["answer_source"] == "live"
    assert records["bad"]["answer_source"] == "fallback"
    assert records["bad"]["fallback_reason"] == FallbackReason.INVALID_ANSWER
    assert "probability" not in records["bad"]


async def test_batch_usage_is_owned_by_exactly_one_record(monkeypatch):
    FakeGateway(answers={"a": 0.9, "b": 0.9}).install(monkeypatch)
    result = await run_decisions(_turn(_candidate("a"), _candidate("b")))
    owners = [record for record in result.evaluations if record.get("usage_owner")]
    assert len(owners) == 1
    assert sum(record.get("usage", {}).get("total_tokens", 0) for record in owners) == 11


# ── the raw-answer cache ─────────────────────────────────────────────────────


async def test_an_identical_question_is_served_from_cache_without_a_request(monkeypatch):
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    await run_decisions(_turn(_candidate()))
    second = await run_decisions(_turn(_candidate()))

    assert len(gateway.batches) == 1
    assert _by_id(second)["outcome"]["answer_source"] == "cache"
    assert _by_id(second)["outcome"]["returned_model"] == "typesafe/jev-1.13.2"
    assert second.requests == 0


@pytest.mark.parametrize(
    "change",
    [
        {"decision_instructions": "A different question?"},
        {"decision_criteria": {"true": "other", "false": "other"}},
        {"decision_state_template": "Only: {{last_message}}"},
    ],
)
async def test_changing_a_classifier_input_invalidates_the_cache(monkeypatch, change):
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    await run_decisions(_turn(_candidate()))
    await run_decisions(_turn(_candidate(**change)))
    assert len(gateway.batches) == 2


async def test_changing_the_model_or_the_configuration_revision_invalidates_the_cache(monkeypatch):
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    await run_decisions(_turn(_candidate()))
    await run_decisions(_turn(_candidate(), config=replace(CONFIG, model="typesafe/jev-2")))
    await run_decisions(_turn(_candidate(), config=replace(CONFIG, revision=2)))
    assert len(gateway.batches) == 3


async def test_a_cache_hit_still_draws_fresh_dice(monkeypatch):
    FakeGateway(answers={"outcome": 0.5}).install(monkeypatch)
    roll = {"decision_resolution": "roll", "decision_threshold": None}
    first = await run_decisions(_turn(_candidate(**roll)))
    draws = {first.evaluations[0]["draw"]}
    for _ in range(20):
        result = await run_decisions(_turn(_candidate(**roll)))
        assert result.evaluations[0]["answer_source"] == "cache"
        draws.add(result.evaluations[0]["draw"])
    # A new occurrence gets its own dice even when the answer was reused.
    assert len(draws) > 1


async def test_a_fallback_is_never_cached(monkeypatch):
    gateway = FakeGateway(answers={}).install(monkeypatch)
    await run_decisions(_turn(_candidate()))
    gateway.answers = {"outcome": 0.9}
    result = await run_decisions(_turn(_candidate()))
    assert _by_id(result)["outcome"]["answer_source"] == "live"
    assert len(gateway.batches) == 2


# ── fallbacks ────────────────────────────────────────────────────────────────


async def test_missing_configuration_falls_back_without_a_request(monkeypatch):
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    result = await run_decisions(_turn(_candidate(), config=DecisionConfig()))

    record = _by_id(result)["outcome"]
    assert record["fallback_reason"] == FallbackReason.NOT_CONFIGURED
    assert record["outcome"] == "false"
    assert record["guidance"] == "outcome failed."
    assert "probability" not in record
    assert gateway.batches == []


async def test_an_unavailable_macro_falls_back_rather_than_inferring_a_member(monkeypatch):
    FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    group = DecisionSnapshot(last_message="hi", description=None, scope="group")
    result = await run_decisions(
        _turn(_candidate(decision_state_template="About {{description}}: {{last_message}}"), snapshot=group)
    )
    assert _by_id(result)["outcome"]["fallback_reason"] == FallbackReason.UNAVAILABLE_CONTEXT


async def test_empty_message_inputs_fall_back_with_empty_input(monkeypatch):
    FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    empty = DecisionSnapshot(last_message="", last_assistant_message="", scope="solo")
    result = await run_decisions(_turn(_candidate(), snapshot=empty))
    assert _by_id(result)["outcome"]["fallback_reason"] == FallbackReason.EMPTY_INPUT


async def test_a_custom_template_supplying_its_own_situation_is_not_empty_input(monkeypatch):
    FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    empty = DecisionSnapshot(last_message="", last_assistant_message="", char="Maren", scope="solo")
    result = await run_decisions(
        _turn(_candidate(decision_state_template="{{char}} stands alone in the dark."), snapshot=empty)
    )
    assert _by_id(result)["outcome"]["answer_source"] == "live"


async def test_an_oversized_state_falls_back_instead_of_being_truncated(monkeypatch):
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    huge = DecisionSnapshot(last_message="x" * 20_000, last_assistant_message="y", scope="solo")
    result = await run_decisions(_turn(_candidate(), snapshot=huge))

    assert _by_id(result)["outcome"]["fallback_reason"] == FallbackReason.OVERSIZED_INPUT
    assert gateway.batches == []
    # Nothing is sent, and the record keeps the measurement rather than the prose:
    # truncating would drop a possibly decisive fact, and echoing an over-limit
    # state onto the reply would store exactly the bytes the limit exists to avoid.
    record = _by_id(result)["outcome"]
    assert record["rendered_state"] == ""
    assert record["oversize_state_bytes"] > record["state_limit"]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ReadTimeout("slow"), FallbackReason.TIMEOUT),
        (DecisionTransportError("unreadable"), FallbackReason.TRANSPORT_FAILURE),
        (httpx.ConnectError("refused"), FallbackReason.TRANSPORT_FAILURE),
    ],
)
async def test_transport_problems_fall_back_for_their_whole_batch(monkeypatch, error, reason):
    gateway = FakeGateway(answers={"a": 0.9, "b": 0.9}).install(monkeypatch)
    gateway.error = error
    result = await run_decisions(_turn(_candidate("a"), _candidate("b")))

    assert {record["fallback_reason"] for record in result.evaluations} == {reason}
    # No retry and no split-and-retry in the interactive path.
    assert len(gateway.batches) == 1


async def test_a_provider_rejection_falls_back_without_extra_attempts(monkeypatch):
    request = httpx.Request("POST", CONFIG.url)
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    gateway.error = LLMCallError(
        "HTTP 429",
        request=request,
        response=httpx.Response(429, request=request),
        sentence="rate limited",
        body="",
        host="example.test",
        model=CONFIG.model,
    )
    result = await run_decisions(_turn(_candidate()))
    assert _by_id(result)["outcome"]["fallback_reason"] == FallbackReason.TRANSPORT_FAILURE
    assert len(gateway.batches) == 1


# ── skips are not fallbacks ──────────────────────────────────────────────────


async def test_a_resting_decision_is_skipped_with_no_request_and_no_guidance(monkeypatch):
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    result = await run_decisions(_turn(_candidate(cooldown_turns=3), prior_cooldowns={"outcome": 2}))

    assert result.evaluations == []
    assert result.skipped == [
        {"fragment_id": "outcome", "fragment_label": "Outcome", "source": "global", "reason": SkipReason.RESTING}
    ]
    assert result.guidance == ""
    assert gateway.batches == []
    # Resting is silence, not a remembered answer: the timer still ages.
    assert result.cooldowns == {"outcome": 1}


async def test_an_unapproved_card_decision_is_skipped_before_cache_or_replay(monkeypatch):
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    # Warm the cache through an approved run, then revoke.
    await run_decisions(_turn(_candidate(card_id="card-1"), approved_cards=frozenset({"card-1"})))
    result = await run_decisions(_turn(_candidate(card_id="card-1")))

    assert result.evaluations == []
    assert result.skipped[0]["reason"] == SkipReason.NOT_APPROVED
    assert result.skipped[0]["source"] == "card:card-1"
    assert len(gateway.batches) == 1  # the approved run only


# ── budgets ──────────────────────────────────────────────────────────────────


async def test_over_budget_decisions_use_their_default_and_stay_visible(monkeypatch):
    ids = [f"q{i}" for i in range(MAX_DECISIONS_PER_EXCHANGE + 2)]
    FakeGateway(answers=dict.fromkeys(ids, 0.9)).install(monkeypatch)
    result = await run_decisions(_turn(*(_candidate(fragment_id) for fragment_id in ids)))

    records = _by_id(result)
    assert len(records) == len(ids)
    over = [record for record in result.evaluations if record.get("fallback_reason") == FallbackReason.BUDGET_EXHAUSTED]
    assert [record["fragment_id"] for record in over] == ids[MAX_DECISIONS_PER_EXCHANGE:]


async def test_one_card_cannot_take_more_than_its_share(monkeypatch):
    ids = [f"q{i}" for i in range(MAX_DECISIONS_PER_CARD + 2)]
    FakeGateway(answers=dict.fromkeys(ids, 0.9)).install(monkeypatch)
    result = await run_decisions(
        _turn(*(_candidate(fragment_id, card_id="c") for fragment_id in ids), approved_cards=frozenset({"c"}))
    )

    over = [record for record in result.evaluations if record.get("fallback_reason") == FallbackReason.BUDGET_EXHAUSTED]
    assert [record["fragment_id"] for record in over] == ids[MAX_DECISIONS_PER_CARD:]


async def test_the_request_attempt_cap_falls_the_rest_back(monkeypatch):
    monkeypatch.setattr(stage_module, "MAX_REQUEST_ATTEMPTS", 2)
    ids = [f"q{i}" for i in range(4)]
    gateway = FakeGateway(answers=dict.fromkeys(ids, 0.9)).install(monkeypatch)
    # One distinct state each, so grouping cannot merge them.
    result = await run_decisions(
        _turn(*(_candidate(i, decision_state_template=f"State {i}: {{{{last_message}}}}") for i in ids))
    )
    assert len(gateway.batches) == 2
    exhausted = [r for r in result.evaluations if r.get("fallback_reason") == FallbackReason.BUDGET_EXHAUSTED]
    assert len(exhausted) == 2


async def test_an_exhausted_stage_budget_stops_further_requests(monkeypatch):
    monkeypatch.setattr(stage_module, "STAGE_BUDGET_SECONDS", 0.0)
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    result = await run_decisions(_turn(_candidate()))
    assert gateway.batches == []
    assert _by_id(result)["outcome"]["fallback_reason"] == FallbackReason.BUDGET_EXHAUSTED


# ── cancellation ─────────────────────────────────────────────────────────────


async def test_a_stop_propagates_rather_than_producing_fallback_guidance(monkeypatch):
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    gateway.error = DecisionCancelled("stopped")
    abort = AbortToken()
    with pytest.raises(DecisionCancelled):
        await run_decisions(_turn(_candidate()), abort=abort)


async def test_a_stop_before_the_first_request_asks_for_nothing(monkeypatch):
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    abort = AbortToken()
    abort.abort()
    with pytest.raises(DecisionCancelled):
        await run_decisions(_turn(_candidate()), abort=abort)
    assert gateway.batches == []


# ── replay ───────────────────────────────────────────────────────────────────


async def _record_for(candidate: DecisionCandidate, monkeypatch, probability: float = 0.9):
    FakeGateway(answers={candidate.definition.fragment_id: probability}).install(monkeypatch)
    result = await run_decisions(_turn(candidate))
    RAW_ANSWER_CACHE.clear()
    return result.evaluations


async def test_an_identical_regeneration_replays_without_a_request(monkeypatch):
    candidate = _candidate(decision_resolution="roll", decision_threshold=None)
    original = await _record_for(candidate, monkeypatch, probability=0.5)

    gateway = FakeGateway(answers={"outcome": 0.5}).install(monkeypatch)
    replayed = await run_decisions(_turn(candidate, replay_records=tuple(original)))

    record = _by_id(replayed)["outcome"]
    assert gateway.batches == []  # cold cache, and still no call
    assert record["answer_source"] == "replay"
    assert record["outcome"] == original[0]["outcome"]
    assert record["draw"] == original[0]["draw"]
    assert record["occurrence_id"] == original[0]["occurrence_id"]


async def test_editing_only_the_output_changes_the_prompt_with_no_call_and_no_reroll(monkeypatch):
    candidate = _candidate(decision_resolution="roll", decision_threshold=None)
    original = await _record_for(candidate, monkeypatch, probability=1.0)

    edited = _candidate(
        decision_resolution="roll",
        decision_threshold=None,
        label="Renamed",
        injection_label="Renamed",
        decision_outputs={"true": "New words for the same outcome.", "false": ""},
    )
    gateway = FakeGateway(answers={"outcome": 1.0}).install(monkeypatch)
    replayed = await run_decisions(_turn(edited, replay_records=tuple(original)))

    record = _by_id(replayed)["outcome"]
    assert gateway.batches == []
    assert record["answer_source"] == "replay"
    assert record["draw"] == original[0]["draw"]
    assert record["guidance"] == "New words for the same outcome."
    assert replayed.guidance == "**Resolved Decisions**\nRenamed: New words for the same outcome."


async def test_a_changed_question_creates_a_new_occurrence(monkeypatch):
    original = await _record_for(_candidate(), monkeypatch)
    gateway = FakeGateway(answers={"outcome": 0.1}).install(monkeypatch)
    fresh = await run_decisions(_turn(_candidate(decision_instructions="Something else?"), replay_records=tuple(original)))

    record = _by_id(fresh)["outcome"]
    assert gateway.batches == [["outcome"]]
    assert record["answer_source"] == "live"
    assert record["occurrence_id"] != original[0]["occurrence_id"]


async def test_a_changed_resolution_policy_creates_a_new_occurrence(monkeypatch):
    original = await _record_for(_candidate(), monkeypatch)
    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    fresh = await run_decisions(_turn(_candidate(decision_threshold=0.95), replay_records=tuple(original)))

    assert gateway.batches == [["outcome"]]
    assert _by_id(fresh)["outcome"]["outcome"] == "false"


async def test_a_changed_policy_may_still_reuse_a_cached_answer_but_rerolls(monkeypatch):
    gateway = FakeGateway(answers={"outcome": 0.5}).install(monkeypatch)
    original = (await run_decisions(_turn(_candidate()))).evaluations  # warms the cache

    roll = _candidate(decision_resolution="roll", decision_threshold=None)
    replayed = await run_decisions(_turn(roll, replay_records=tuple(original)))

    record = _by_id(replayed)["outcome"]
    assert len(gateway.batches) == 1  # the cache served the second
    assert record["answer_source"] == "cache"
    assert "draw" in record


async def test_a_fallback_record_replays_as_a_fallback(monkeypatch):
    FakeGateway(answers={}).install(monkeypatch)
    original = (await run_decisions(_turn(_candidate()))).evaluations
    assert original[0]["fallback_reason"] == FallbackReason.INVALID_ANSWER

    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    replayed = await run_decisions(_turn(_candidate(), replay_records=tuple(original)))

    record = _by_id(replayed)["outcome"]
    # Regeneration alone does not retry a failed classification.
    assert gateway.batches == []
    assert record["answer_source"] == "replay"
    assert record["fallback_reason"] == FallbackReason.INVALID_ANSWER
    assert record["outcome"] == "false"


async def test_a_lost_branch_anchor_re_asks_and_says_why(monkeypatch):
    original = await _record_for(_candidate(), monkeypatch)
    copied = envelope(original, [])
    from backend.pipeline.passes.decisions import remap_anchors

    orphaned = remap_anchors(copied, {})["evaluations"]

    gateway = FakeGateway(answers={"outcome": 0.9}).install(monkeypatch)
    fresh = await run_decisions(_turn(_candidate(), replay_records=tuple(orphaned)))

    record = _by_id(fresh)["outcome"]
    assert gateway.batches == [["outcome"]]
    assert record["answer_source"] == "live"
    assert record["replay_invalidated"] == FallbackReason.MISSING_ANCHOR


# ── the empty stage ──────────────────────────────────────────────────────────


def test_an_exchange_with_no_candidates_has_no_work():
    assert not stage_has_work(_turn())
    assert stage_has_work(_turn(_candidate()))


async def test_a_stage_with_no_candidates_still_ages_cooldowns():
    result = await run_decisions(_turn(prior_cooldowns={"gone": 2}))
    assert result.cooldowns == {"gone": 1}
    assert result.evaluations == [] and result.guidance == ""


async def test_stage_results_are_published_only_once_the_stage_finishes(monkeypatch):
    # Two states, so two sequential requests; the second's answer must not be
    # observable before the first has also landed. The result object is the only
    # publication point, which is what makes that true by construction.
    started = asyncio.Event()

    async def _slow(self, state, questions, *, timeout=None, abort=None):  # noqa: ANN001
        started.set()
        await asyncio.sleep(0)
        return DecisionResponse(answers={q.key: 0.9 for q in questions}, returned_model="m")

    monkeypatch.setattr(stage_module.DecisionClient, "decide", _slow)
    task = asyncio.ensure_future(
        run_decisions(_turn(_candidate("a"), _candidate("b", decision_state_template="Other: {{last_message}}")))
    )
    await started.wait()
    assert not task.done()
    result = await task
    assert len(result.evaluations) == 2
