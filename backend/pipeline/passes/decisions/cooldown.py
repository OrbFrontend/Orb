from collections.abc import Mapping, Sequence
from typing import Any

from ....core import DecisionDefinition


def branch_baseline(history: Sequence[Mapping[str, Any]], *, before_exchange_id: str | None = None) -> dict[str, int]:
    for message in reversed(history):
        if message.get("role") != "assistant":
            continue
        if before_exchange_id is not None and str(message.get("exchange_id") or "") == before_exchange_id:
            continue
        return {str(key): int(value) for key, value in (message.get("decision_cooldowns") or {}).items()}
    return {}


def blocked(state: Mapping[str, int]) -> frozenset[str]:
    return frozenset(fragment_id for fragment_id, turns in state.items() if turns >= 1)


def advance(
    prior: Mapping[str, int], evaluated: Sequence[str] | set[str], definitions: Sequence[DecisionDefinition]
) -> dict[str, int]:
    state = {fragment_id: turns - 1 for fragment_id, turns in prior.items() if turns > 1}
    cooldowns = {definition.fragment_id: definition.cooldown_turns for definition in definitions}
    for fragment_id in evaluated:
        if turns := cooldowns.get(fragment_id, 0):
            state[fragment_id] = turns
    return state
