"""Branch-aware per-fragment Director cooldown state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def branch_baseline(history: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Return the latest assistant message's cooldown snapshot."""
    previous = next((message for message in reversed(history) if message["role"] == "assistant"), None)
    return dict(previous.get("fragment_cooldowns") or {}) if previous else {}


def blocked(state: Mapping[str, int]) -> frozenset[str]:
    """Return fragment ids unavailable on the current turn."""
    return frozenset(fragment_id for fragment_id, turns in state.items() if turns >= 1)


def advance(
    prior: Mapping[str, int],
    fired: Sequence[str] | set[str],
    fragments: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Age prior cooldowns, then start cooldowns for fragments fired now."""
    state = {fragment_id: turns - 1 for fragment_id, turns in prior.items() if turns > 1}
    cooldowns = {fragment["id"]: int(fragment.get("cooldown_turns") or 0) for fragment in fragments}
    for fragment_id in fired:
        turns = cooldowns.get(fragment_id, 0)
        if turns > 0:
            state[fragment_id] = turns
    return state


def fire(state: Mapping[str, int], fired: Sequence[str] | set[str], cooldowns: Mapping[str, int]) -> dict[str, int]:
    """Start cooldowns for fragments that fired after the Director's aging step.

    State fragments updated through the ``update_state`` tool fire later in the
    turn than :func:`advance` runs; this adds their cooldowns to the same snapshot
    without aging anything a second time.
    """
    out = dict(state)
    for fragment_id in fired:
        turns = int(cooldowns.get(fragment_id) or 0)
        if turns > 0:
            out[fragment_id] = turns
    return out
