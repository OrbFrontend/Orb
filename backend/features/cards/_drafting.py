"""Shared forced-call helpers for card profile and sheet drafts."""

from __future__ import annotations

import re
from typing import Any

from ...core import ChatMessage
from ...inference import LLMClient, parse_tool_calls

_WHITESPACE_RE = re.compile(r"\s+")

# Drafted fields are macro-resolved when a turn is assembled.
BRACES = ("{", "}")


def normalize(text: str) -> str:
    """Collapse a parsed field to a single line."""
    return _WHITESPACE_RE.sub(" ", text).strip()


async def forced_draft(
    client: LLMClient,
    model: str,
    *,
    system: str,
    user: str,
    tool: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any] | None:
    """One forced call to *tool*, drained. Returns its arguments, or ``None``.

    ``None`` means the endpoint answered without calling the tool it was given;
    each caller turns that into its own ``*Unavailable`` sentence, because the
    two describe different things to the user.

    Hyperparameters are hardcoded and deliberately do *not* go through
    ``core.extract_hyperparams``: that path exists for prose the user asked to
    be rewritten and wants their writing preset applied to, while a roleplay
    preset at ``temperature: 1.15`` would embellish a summarization call.
    """
    name = tool["function"]["name"]
    messages: list[ChatMessage] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    response: dict = {}
    async for event in client.complete(
        messages=messages,
        model=model or "",
        tools=[tool],
        tool_choice={"type": "function", "function": {"name": name}},
        temperature=0.2,
        max_tokens=max_tokens,
    ):
        if event.get("type") == "done":
            response = event.get("message") or {}
    return next((call.get("arguments") or {} for call in parse_tool_calls(response) if call.get("name") == name), None)
