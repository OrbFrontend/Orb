"""Branch-aware decision cooldown state.

Deliberately a separate module from ``passes/director/cooldown.py``, and
deliberately not factored together with it. The arithmetic looks the same; the
state does not mean the same thing:

* a Director fragment's cooldown starts when the Director **fired** it, and its
  snapshot advances once per reply;
* a decision's cooldown starts when the decision was **evaluated at all** --
  live, cached, replayed, or fallen back -- and advances once per completed
  *exchange*, regardless of the answer or whether the output was empty.

Sharing one helper would make those two sentences one sentence, and the first
group chat would find out which one it meant.

``cooldown_turns = N`` therefore means: skip the next N completed exchanges
after an evaluation. The editor says exactly that, rather than promising a
particular API call frequency, because a cooldown bounds evaluations and a
cached or replayed evaluation costs no call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ....core import DecisionDefinition


def branch_baseline(history: Sequence[Mapping[str, Any]], *, before_exchange_id: str | None = None) -> dict[str, int]:
    """The decision cooldown state this turn starts from.

    The latest assistant message's snapshot, which makes the baseline
    branch-correct without a per-conversation row.

    *before_exchange_id* is what a group regeneration needs. A later speaker's
    immediate parent is an earlier reply *in the same exchange*, and that reply
    already carries the exchange's advanced snapshot -- so reusing it would
    charge the exchange's cooldowns twice. Naming the target's exchange skips
    every reply in it and lands on the state from before the exchange began.
    """
    for message in reversed(history):
        if message.get("role") != "assistant":
            continue
        if before_exchange_id is not None and str(message.get("exchange_id") or "") == before_exchange_id:
            continue
        return {str(key): int(value) for key, value in (message.get("decision_cooldowns") or {}).items()}
    return {}


def blocked(state: Mapping[str, int]) -> frozenset[str]:
    """Decision ids resting this exchange.

    A resting decision is *skipped*: no request, no outcome injection, no
    fallback, and no reuse of an earlier turn's guidance. Resting is silence,
    not a remembered answer.
    """
    return frozenset(fragment_id for fragment_id, turns in state.items() if turns >= 1)


def advance(
    prior: Mapping[str, int], evaluated: Sequence[str] | set[str], definitions: Sequence[DecisionDefinition]
) -> dict[str, int]:
    """Age *prior* by one exchange, then start cooldowns for what was evaluated.

    Called for every completed exchange that retains a reply, including one with
    no eligible decisions at all -- otherwise disabling every decision would
    freeze the timers of the ones still resting, and re-enabling them would find
    a cooldown that never expired.

    A decision whose definition is gone (deleted, or a card no longer in the
    scene) ages out of the map rather than being carried: ``cooldowns`` cannot
    name it, so nothing re-arms it.
    """
    state = {fragment_id: turns - 1 for fragment_id, turns in prior.items() if turns > 1}
    cooldowns = {definition.fragment_id: definition.cooldown_turns for definition in definitions}
    for fragment_id in evaluated:
        turns = cooldowns.get(fragment_id, 0)
        if turns > 0:
            state[fragment_id] = turns
    return state


__all__ = ["advance", "blocked", "branch_baseline"]
