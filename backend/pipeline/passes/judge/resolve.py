from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

TRUE = "true"
FALSE = "false"

# The resolutions that draw against the classifier's odds instead of reading the
# outcome straight off its answer. Regeneration keeps their answer but draws again.
DRAWN_RESOLUTIONS = frozenset({"roll", "weighted", "gated"})


class SkipReason:
    """Reasons a decision may be skipped for a turn."""

    RESTING = "resting"
    INVALID_DEFINITION = "invalid_definition"
    NOT_CONFIGURED = "not_configured"
    EMPTY_INPUT = "empty_input"
    UNAVAILABLE_CONTEXT = "unavailable_context"
    OVERSIZED_INPUT = "oversized_input"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TRANSPORT_FAILURE = "transport_failure"
    TIMEOUT = "timeout"
    INVALID_ANSWER = "invalid_answer"
    LOW_CONFIDENCE = "low_confidence"
    MISSING_ANCHOR = "missing_anchor"


# Routine skips do not produce a failure notice.
ROUTINE_REASONS = frozenset({SkipReason.RESTING, SkipReason.LOW_CONFIDENCE, SkipReason.EMPTY_INPUT})


def resolve_threshold(probability: float, threshold: float) -> str:
    return TRUE if probability >= threshold else FALSE


def resolve_roll(probability: float, draw: float) -> str:
    return TRUE if draw < probability else FALSE


def draw_uniform() -> float:
    return random.random()  # nosec B311 -- narrative choice, not security


def resolve_argmax(probabilities: Mapping[str, float], keys: Sequence[str]) -> str:
    """Resolve ties in authored order, not provider/dict ordering."""
    return max(keys, key=lambda key: probabilities[key])


def resolve_weighted(probabilities: Mapping[str, float], keys: Sequence[str], draw: float) -> str:
    cumulative = 0.0
    for key in keys:
        cumulative += probabilities[key]
        if draw < cumulative:
            return key
    # A normalized provider distribution may finish at 0.999999 through float
    # addition. The final authored option owns that tiny tail.
    return keys[-1]


def gate_holds(probabilities: Mapping[str, float], keys: Sequence[str]) -> bool:
    """Whether the first authored option is likeliest and blocks the draw."""
    return resolve_argmax(probabilities, keys) == keys[0]


def resolve_gated(probabilities: Mapping[str, float], keys: Sequence[str], draw: float) -> str:
    """Draw among every option but the gate, on odds rescaled to exclude it.

    Only for a gate that did not hold, so some later option outweighs it and the
    rescaling total is positive.
    """
    rest = keys[1:]
    total = sum(probabilities[key] for key in rest)
    return resolve_weighted({key: probabilities[key] / total for key in rest}, rest, draw)


def resolve_nearest(mean: float, keys: Sequence[str]) -> str:
    # Python's round uses bankers' rounding. A score scale's midpoint belongs to
    # the upper level, which is the author-facing interpretation of "nearest".
    index = int(mean + 0.5)
    return keys[min(max(index, 0), len(keys) - 1)]
