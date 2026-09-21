"""Turn a probability into an outcome, or say why there isn't one.

Nothing here reaches a model prompt. The probability, the draw, and the reason a
fallback fired are Inspector facts: a rolled outcome is presented to the Writer
as an authored story constraint, not as a dice result, and a threshold outcome
is presented as the selected guidance. Using a classifier's probability as a
narrative roll weight is an Orb policy, not a claim that fictional outcomes have
empirically calibrated odds.
"""

from __future__ import annotations

import random

TRUE = "true"
FALSE = "false"


class FallbackReason:
    """Why a decision used its authored fallback instead of an answer.

    Strings, not an enum, because they are persisted in evaluation records and
    rendered in the Inspector: a reason has to survive a schema round trip and
    read as English when it gets there.
    """

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
    """Why a decision did not run at all.

    A skip is not a fallback. A disabled, unapproved or resting decision
    contributes nothing -- no request, no injection, no remembered guidance --
    and the Inspector records it without inventing an outcome for it.
    """

    DISABLED = "disabled"
    NOT_APPROVED = "not_approved"
    RESTING = "resting"


def resolve_threshold(probability: float, threshold: float) -> str:
    """``true`` when ``p >= threshold``.

    The equality rule is part of the contract and is tested explicitly: an
    author who sets 0.5 and gets exactly 0.5 has to be able to say which way it
    goes without reading this file.
    """
    return TRUE if probability >= threshold else FALSE


def resolve_roll(probability: float, draw: float) -> str:
    """``true`` when ``u < p``, for one uniform draw ``u`` in ``[0, 1)``.

    Strict ``<`` against a half-open draw is what makes the ends absolute:
    ``p = 0`` always fails (no ``u`` is below zero) and ``p = 1`` always succeeds
    (every ``u`` is below one). A weighted roll that could still betray a
    certainty would be a worse tool than a threshold.
    """
    return TRUE if draw < probability else FALSE


def draw_uniform() -> float:
    """One uniform draw in ``[0, 1)`` for one decision occurrence.

    Per occurrence, not per answer: a new occurrence gets a fresh draw even when
    its classifier response came from cache, and repeated regeneration of an
    unchanged reply replays the stored draw rather than rolling again.
    """
    # Narrative dice, not a security decision, so the fast PRNG is the right
    # one. (The prose stays off the nosec line: bandit parses everything after
    # the code as test ids and warns on each word.)
    return random.random()  # nosec B311


__all__ = ["FALSE", "TRUE", "FallbackReason", "SkipReason", "draw_uniform", "resolve_roll", "resolve_threshold"]
