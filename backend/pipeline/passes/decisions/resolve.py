from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

TRUE = "true"
FALSE = "false"


class FallbackReason:
    NOT_CONFIGURED = "not_configured"
    INVALID_DEFINITION = "invalid_definition"
    EMPTY_INPUT = "empty_input"
    UNAVAILABLE_CONTEXT = "unavailable_context"
    OVERSIZED_INPUT = "oversized_input"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TRANSPORT_FAILURE = "transport_failure"
    TIMEOUT = "timeout"
    INVALID_ANSWER = "invalid_answer"
    INVALID_FACET_ANSWER = "invalid_facet_answer"
    LOW_CONFIDENCE = "low_confidence"
    MISSING_ANCHOR = "missing_anchor"


class SkipReason:
    NOT_APPROVED = "not_approved"
    RESTING = "resting"
    # Shares its spelling with the fallback of the same name on purpose: an author
    # reading either one is being told the same thing about their definition. They
    # stay separate constants because a skip has no outcome and a fallback does.
    INVALID_DEFINITION = "invalid_definition"


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


def resolve_nearest(mean: float, keys: Sequence[str]) -> str:
    # Python's round uses bankers' rounding. A score scale's midpoint belongs to
    # the upper level, which is the author-facing interpretation of "nearest".
    index = int(mean + 0.5)
    return keys[min(max(index, 0), len(keys) - 1)]
