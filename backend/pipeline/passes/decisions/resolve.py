from __future__ import annotations

import random

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
    MISSING_ANCHOR = "missing_anchor"


class SkipReason:
    NOT_APPROVED = "not_approved"
    RESTING = "resting"


def resolve_threshold(probability: float, threshold: float) -> str:
    return TRUE if probability >= threshold else FALSE


def resolve_roll(probability: float, draw: float) -> str:
    return TRUE if draw < probability else FALSE


def draw_uniform() -> float:
    return random.random()  # nosec B311 -- narrative choice, not security
