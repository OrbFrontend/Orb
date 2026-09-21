"""Unit tests for branch-aware Director fragment cooldown state."""

from __future__ import annotations

from backend.pipeline.passes.director import cooldown


def test_branch_baseline_uses_last_assistant_snapshot():
    history = [
        {"role": "assistant", "fragment_cooldowns": {"tense": 3}},
        {"role": "user"},
        {"role": "assistant", "fragment_cooldowns": {"tense": 1}},
        {"role": "user"},
    ]
    assert cooldown.branch_baseline(history) == {"tense": 1}
    assert cooldown.branch_baseline([]) == {}
    assert cooldown.branch_baseline([{"role": "user"}]) == {}


def test_blocked_includes_positive_entries_only():
    assert cooldown.blocked({"tense": 2, "ready": 0}) == frozenset({"tense"})


def test_advance_decrements_drops_and_overlays_fired_fragments():
    fragments = [
        {"id": "tense", "cooldown_turns": 3},
        {"id": "pacing", "cooldown_turns": 2},
        {"id": "off", "cooldown_turns": 0},
    ]
    assert cooldown.advance({"tense": 2, "unknown": 1}, ["pacing", "off", "speaking_plan"], fragments) == {
        "tense": 1,
        "pacing": 2,
    }


def test_cooldown_expires_after_exact_number_of_resting_turns():
    fragments = [{"id": "tense", "cooldown_turns": 3}]
    state = cooldown.advance({}, ["tense"], fragments)
    assert state == {"tense": 3}
    for expected in (2, 1, 0):
        assert "tense" in cooldown.blocked(state)
        state = cooldown.advance(state, [], fragments)
        assert state.get("tense", 0) == expected
    assert "tense" not in cooldown.blocked(state)
