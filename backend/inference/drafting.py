"""Drain forced tool calls for feature-level model requests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..core import ChatMessage
from .client import LLMClient, parse_tool_calls, reasoning_cfg

_WHITESPACE_RE = re.compile(r"\s+")

# Drafted fields are macro-resolved when a turn is assembled.
BRACES = ("{", "}")


class ReplyCutOff(RuntimeError):
    """A forced reply stopped at its ``max_tokens``, so its arguments are incomplete."""


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
    reasoning_on: bool,
) -> dict[str, Any] | None:
    """Make one forced tool call and return its arguments, or ``None`` if absent.

    Raises :class:`ReplyCutOff` when the reply stopped at *max_tokens*: arguments
    salvaged from a cut reply can look complete while missing their tail.
    """
    name = tool["function"]["name"]
    messages: list[ChatMessage] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    response = await forced_turn(
        client,
        model,
        messages=messages,
        tools=[tool],
        forced=name,
        max_tokens=max_tokens,
        reasoning_on=reasoning_on,
    )
    if response.get("finish_reason") == "length":
        raise ReplyCutOff
    return next((call.get("arguments") or {} for call in parse_tool_calls(response) if call.get("name") == name), None)


async def forced_turn(
    client: LLMClient,
    model: str,
    *,
    messages: Sequence[Mapping[str, Any]],
    tools: list[dict[str, Any]],
    forced: str,
    max_tokens: int,
    reasoning_on: bool,
) -> dict[str, Any]:
    """Make one forced tool call and return its complete response."""
    response: dict[str, Any] = {}
    async for event in client.complete(
        messages=messages,
        model=model or "",
        tools=tools,
        tool_choice={"type": "function", "function": {"name": forced}},
        temperature=0.2,
        max_tokens=max_tokens,
        **reasoning_cfg(reasoning_on),
    ):
        if event.get("type") == "done":
            response = event.get("message") or {}
    return response
