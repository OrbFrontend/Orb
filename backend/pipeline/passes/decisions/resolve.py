from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

TRUE = "true"
FALSE = "false"

# The resolutions that draw against the classifier's odds instead of reading the
# outcome straight off its answer. Regeneration keeps their answer but draws again.
DRAWN_RESOLUTIONS = frozenset({"roll", "weighted"})


class SkipReason:
    """Why a decision produced nothing this turn.

    One vocabulary, because there is one outcome for a decision that cannot
    answer: it is skipped and the turn goes on without it. Nothing is injected
    and nothing is invented, so an author reading a reason is never being told
    about a value that reached the story.
    """

    NOT_APPROVED = "not_approved"
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


# The reasons that mean something went wrong, as opposed to the routine ones an
# author configured on purpose. Only these are worth interrupting anybody about.
FAILURE_REASONS = frozenset(
    {
        SkipReason.NOT_CONFIGURED,
        SkipReason.INVALID_DEFINITION,
        SkipReason.EMPTY_INPUT,
        SkipReason.UNAVAILABLE_CONTEXT,
        SkipReason.OVERSIZED_INPUT,
        SkipReason.BUDGET_EXHAUSTED,
        SkipReason.TRANSPORT_FAILURE,
        SkipReason.TIMEOUT,
        SkipReason.INVALID_ANSWER,
    }
)


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
