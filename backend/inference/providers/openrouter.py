"""OpenRouter, which routes each model to one of several upstream providers."""

from __future__ import annotations

from ..ledger import PairFacts
from .base import Dialect


def _is_tool_choice_unsupported(status: int, text: str) -> bool:
    """Return ``True`` when the body says no ``tool_choice`` value is routed.

    Matches "No endpoints found that support the provided 'tool_choice' value." -- meaning the routed provider rejects all
    ``tool_choice`` values. Kept narrow so genuine 404s (bad model id, etc.) don't match.
    """
    low = text.lower()
    return status in {400, 404} and "tool_choice" in low and "no endpoints found" in low


class OpenRouter(Dialect):
    name = "openrouter"

    def matches(self, url: str) -> bool:
        return "openrouter.ai" in url.lower()

    def recover(self, facts: PairFacts, body: dict, status: int, text: str, model: str) -> str | None:
        # The routed provider rejects tool_choice entirely. Recovery is to drop the param and retry; the 404 lands before any SSE
        # event so the retry is clean, and later calls drop it up front.
        if "tool_choice" in body and _is_tool_choice_unsupported(status, text):
            facts.tool_choice_unsupported = True
            tc = body.pop("tool_choice")
            return f"Model {model} rejected tool_choice={tc!r}; retrying without it."
        return None
