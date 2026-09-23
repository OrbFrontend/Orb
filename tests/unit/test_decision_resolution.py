"""Resolution rules, cooldown semantics, and the two fingerprints.

The threshold equality rule and the roll's absolute ends are contract, not
implementation detail: an author who sets 0.5 and gets exactly 0.5 has to be
able to say which way it goes, and a weighted roll that could betray a certainty
would be a worse tool than a threshold.
"""

from __future__ import annotations

from backend.core import DEFAULT_STATE_TEMPLATE, parse_decision_definition
from backend.inference import DecisionQuestion
from backend.pipeline.passes.judge import (
    EVALUATIONS_VERSION,
    advance_decision_cooldowns,
    decision_cooldown_baseline,
    decision_guidance_block,
    envelope,
    raw_request_fingerprint,
    remap_anchors,
    resolution_policy_fingerprint,
    resolve_argmax,
    resolve_nearest,
    resolve_roll,
    resolve_threshold,
    resolve_weighted,
    resting_decisions,
    stored_evaluations,
)
from backend.pipeline.passes.judge.records import (
    invalidated_anchor,
    matching_replay,
)
from backend.pipeline.passes.judge.resolve import draw_uniform


def _definition(**overrides):
    row = {
        "id": "outcome",
        "label": "Outcome",
        "injection_label": "Outcome",
        "field_type": "decision",
        "cooldown_turns": overrides.pop("cooldown_turns", 0),
        "decision_type": "noul",
        "decision_placement": "before_director",
        "decision_state_template": DEFAULT_STATE_TEMPLATE,
        "decision_instructions": "Does Alric prevail?",
        "decision_criteria": {"true": "He wins.", "false": "He loses."},
        "decision_outputs": {"true": "He holds the door.", "false": "He is driven back."},
        "decision_resolution": "threshold",
        "decision_threshold": 0.5,
    }
    row.update(overrides)
    definition = parse_decision_definition(row)
    assert definition is not None
    return definition


# ── threshold ────────────────────────────────────────────────────────────────


def test_threshold_resolves_true_at_exact_equality():
    assert resolve_threshold(0.5, 0.5) == "true"
    assert resolve_threshold(0.4999999, 0.5) == "false"
    assert resolve_threshold(0.5000001, 0.5) == "true"


def test_threshold_ends_behave():
    assert resolve_threshold(0.0, 0.0) == "true"
    assert resolve_threshold(1.0, 1.0) == "true"
    assert resolve_threshold(0.0, 1.0) == "false"


# ── roll ─────────────────────────────────────────────────────────────────────


def test_probability_zero_always_fails_and_one_always_succeeds():
    for draw in (0.0, 0.5, 0.999999):
        assert resolve_roll(0.0, draw) == "false"
        assert resolve_roll(1.0, draw) == "true"


def test_roll_is_strictly_below_the_probability():
    assert resolve_roll(0.5, 0.4999) == "true"
    assert resolve_roll(0.5, 0.5) == "false"


def test_draws_are_in_the_half_open_unit_interval():
    draws = [draw_uniform() for _ in range(200)]
    assert all(0.0 <= draw < 1.0 for draw in draws)
    # Not a distribution test: only that this is a draw and not a constant.
    assert len(set(draws)) > 1


def test_argmax_and_nearest_are_distinct_score_policies():
    probabilities = {"0": 0.0, "1": 0.34, "2": 0.31, "3": 0.35}
    keys = tuple(probabilities)
    assert resolve_argmax(probabilities, keys) == "3"
    assert resolve_nearest(2.01, keys) == "2"


def test_weighted_resolution_uses_one_draw_against_authored_order():
    probabilities = {"a": 0.2, "b": 0.3, "c": 0.5}
    assert resolve_weighted(probabilities, tuple(probabilities), 0.0) == "a"
    assert resolve_weighted(probabilities, tuple(probabilities), 0.2) == "b"
    assert resolve_weighted(probabilities, tuple(probabilities), 0.99) == "c"


# ── decision cooldowns ───────────────────────────────────────────────────────


def test_baseline_reads_the_latest_assistant_snapshot():
    history = [
        {"role": "assistant", "decision_cooldowns": {"outcome": 3}},
        {"role": "user"},
        {"role": "assistant", "decision_cooldowns": {"outcome": 1}},
    ]
    assert decision_cooldown_baseline(history) == {"outcome": 1}
    assert decision_cooldown_baseline([]) == {}


def test_a_group_regeneration_steps_over_its_own_exchange():
    # A later speaker's immediate parent already carries the exchange's advanced
    # snapshot, so reusing it would charge the exchange's cooldowns twice.
    history = [
        {"role": "assistant", "exchange_id": "e1", "decision_cooldowns": {"outcome": 0}},
        {"role": "user", "exchange_id": "e2"},
        {"role": "assistant", "exchange_id": "e2", "decision_cooldowns": {"outcome": 3}},
        {"role": "assistant", "exchange_id": "e2", "decision_cooldowns": {"outcome": 3}},
    ]
    assert decision_cooldown_baseline(history, before_exchange_id="e2") == {"outcome": 0}
    assert decision_cooldown_baseline(history) == {"outcome": 3}


def test_cooldown_skips_exactly_n_completed_exchanges():
    definitions = [_definition(cooldown_turns=3)]
    state = advance_decision_cooldowns({}, ["outcome"], definitions)
    assert state == {"outcome": 3}
    for expected in (2, 1, 0):
        assert "outcome" in resting_decisions(state)
        state = advance_decision_cooldowns(state, [], definitions)
        assert state.get("outcome", 0) == expected
    assert "outcome" not in resting_decisions(state)


def test_cooldowns_age_on_an_exchange_with_nothing_eligible():
    # Disabling every decision must not freeze the timers of the resting ones.
    assert advance_decision_cooldowns({"outcome": 2}, [], []) == {"outcome": 1}


def test_a_zero_cooldown_decision_never_rests():
    assert advance_decision_cooldowns({}, ["outcome"], [_definition(cooldown_turns=0)]) == {}


def test_a_decision_whose_definition_is_gone_ages_out():
    assert advance_decision_cooldowns({"vanished": 1}, [], []) == {}


# ── fingerprints ─────────────────────────────────────────────────────────────


def _raw(**overrides) -> str:
    args = {
        "model": "typesafe/jev-1.13",
        "state": "Alric shoves the door.",
        "instructions": "Does Alric prevail?",
        "criteria": {"true": "He wins.", "false": "He loses."},
        "question_type": "noul",
    }
    args.update(overrides)
    question = DecisionQuestion("outcome", args["instructions"], args["criteria"], args["question_type"])
    return raw_request_fingerprint(args["model"], args["state"], question)


def test_the_raw_fingerprint_covers_every_classifier_input():
    base = _raw()
    assert _raw(model="other") != base
    assert _raw(state="different") != base
    assert _raw(instructions="different?") != base
    assert _raw(criteria={"true": "He prevails.", "false": "He loses."}) != base
    assert _raw(question_type="score") != base


def test_the_raw_fingerprint_does_not_normalize_prose():
    assert _raw(instructions="does alric prevail?") != _raw()
    assert _raw(instructions="Does  Alric prevail?") != _raw()


def test_the_policy_fingerprint_covers_resolution_and_scope_only():
    base = resolution_policy_fingerprint(_definition(), scope="solo")
    assert resolution_policy_fingerprint(_definition(), scope="group") != base
    assert resolution_policy_fingerprint(_definition(decision_threshold=0.7), scope="solo") != base
    assert resolution_policy_fingerprint(_definition(decision_resolution="roll", decision_threshold=None), scope="solo") != base
    # Labels and authored guidance are not classifier inputs and do not change a
    # resolution, so editing them must cost neither a call nor a reroll.
    assert resolution_policy_fingerprint(_definition(label="Renamed"), scope="solo") == base
    assert resolution_policy_fingerprint(_definition(decision_outputs={"true": "new", "false": ""}), scope="solo") == base


# ── the stored envelope ──────────────────────────────────────────────────────


def test_an_exchange_with_nothing_to_record_stores_no_envelope():
    assert envelope([], []) == {}


def test_a_record_from_a_newer_orb_is_not_replayed():
    future = {"version": EVALUATIONS_VERSION + 1, "evaluations": [{"fragment_id": "outcome"}]}
    assert stored_evaluations(future) == []
    assert stored_evaluations(None) == []


def test_replay_needs_the_fragment_the_request_and_the_policy_to_match():
    record = {
        "fragment_id": "outcome",
        "raw_request_fingerprint": "raw",
        "resolution_policy_fingerprint": "policy",
        "outcome": "true",
    }
    args = {"fragment_id": "outcome", "raw_fingerprint": "raw", "policy_fingerprint": "policy"}
    assert matching_replay([record], **args) is not None
    assert matching_replay([record], **{**args, "raw_fingerprint": "other"}) is None
    assert matching_replay([record], **{**args, "policy_fingerprint": "other"}) is None
    assert matching_replay([record], **{**args, "fragment_id": "other"}) is None


def test_anchors_remap_through_a_copy_and_a_lost_one_is_invalidated():
    stored = envelope(
        [
            {"fragment_id": "kept", "input_branch_anchor": 7},
            {"fragment_id": "lost", "input_branch_anchor": 99},
            {"fragment_id": "none", "input_branch_anchor": None},
        ],
        [{"fragment_id": "resting", "reason": "resting"}],
    )
    remapped = remap_anchors(stored, {7: 70})
    records = {record["fragment_id"]: record for record in remapped["evaluations"]}
    assert records["kept"]["input_branch_anchor"] == 70
    assert records["kept"].get("anchor_invalidated") is None
    assert records["lost"]["input_branch_anchor"] is None
    assert records["lost"]["anchor_invalidated"] is True
    assert records["none"].get("anchor_invalidated") is None
    # Skips are diagnostics and carry no anchor, so they travel unchanged.
    assert remapped["skipped"] == [{"fragment_id": "resting", "reason": "resting"}]

    # An invalidated anchor is never a replay match; it is a reason to re-ask.
    assert matching_replay(remapped["evaluations"], fragment_id="lost", raw_fingerprint="", policy_fingerprint="") is None
    assert invalidated_anchor(remapped["evaluations"], "lost")
    assert not invalidated_anchor(remapped["evaluations"], "kept")


# ── the injected block ───────────────────────────────────────────────────────


def test_guidance_is_labelled_authored_text_and_nothing_about_dice():
    block = decision_guidance_block(
        [
            {
                "fragment_id": "outcome",
                "injection_label": "Doorway",
                "guidance": "He holds the door.",
                "probability": 0.93,
                "draw": 0.12,
                "outcome": "true",
            }
        ]
    )
    assert block == "**Major Decisions**\n\nDoorway: He holds the door."
    assert "0.93" not in block and "0.12" not in block and "true" not in block


def test_an_empty_selected_output_suppresses_the_whole_line():
    assert decision_guidance_block([{"fragment_id": "outcome", "injection_label": "Doorway", "guidance": "   "}]) == ""
    assert decision_guidance_block([]) == ""


def test_guidance_falls_back_through_label_then_id():
    assert "Outcome: x" in decision_guidance_block([{"fragment_id": "outcome", "fragment_label": "Outcome", "guidance": "x"}])
    assert "outcome: x" in decision_guidance_block([{"fragment_id": "outcome", "guidance": "x"}])
