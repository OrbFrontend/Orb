"""Drain forced tool calls for feature-level model requests."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..core import AssistantToolMessage, ChatMessage
from ..core.llm_types import CompletionMessage, ToolResultMessage
from .client import LLMClient, parse_tool_calls, reasoning_cfg, replay_reasoning

_WHITESPACE_RE = re.compile(r"\s+")

# Drafted fields are macro-resolved when a turn is assembled.
BRACES = ("{", "}")


class ReplyCutOff(RuntimeError):
    """A forced reply stopped at its ``max_tokens``, so its arguments are incomplete."""


def normalize(text: str) -> str:
    """Collapse a parsed field to a single line."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def fence_quote(text: str) -> str:
    # Reference text must not be able to close its own fence.
    escaped = text.strip().replace('"""', '\\"\\"\\"')
    return f'"""\n{escaped}\n"""'


def assistant_tool_call(
    response: Mapping[str, Any], name: str, arguments: Mapping[str, Any], call_id: str
) -> AssistantToolMessage:
    # Structured forced calls all come back as ``call_0``; the id is assigned
    # here so every replayed call answers to exactly one result.
    message: AssistantToolMessage = {
        "role": "assistant",
        "content": response.get("content") or "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
        **replay_reasoning(response),
    }
    return message


def tool_result(call_id: str, content: str) -> ToolResultMessage:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


async def forced_draft(
    client: LLMClient, model: str, *, system: str, user: str, tool: dict[str, Any], max_tokens: int, reasoning_on: bool
) -> dict[str, Any] | None:
    """Make one forced tool call and return its arguments, or ``None`` if absent.

    Raises :class:`ReplyCutOff` when the reply stopped at *max_tokens*: arguments
    salvaged from a cut reply can look complete while missing their tail.
    """
    name = tool["function"]["name"]
    messages: list[ChatMessage] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    response = await forced_turn(
        client, model, messages=messages, tools=[tool], forced=name, max_tokens=max_tokens, reasoning_on=reasoning_on
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
    one_call: bool = False,
) -> CompletionMessage:
    """Make one forced tool call and return its complete response.

    *one_call* also asks for no parallel tool calls where the request carries native tools: an endpoint that answers the forced
    choice as ``auto`` may otherwise reply with several calls at once.
    """
    response: CompletionMessage = {}
    async for event in client.complete(
        messages=messages,
        model=model or "",
        tools=tools,
        tool_choice={"type": "function", "function": {"name": forced}},
        temperature=0.2,
        max_tokens=max_tokens,
        **({"single_tool_call": True} if one_call else {}),
        **reasoning_cfg(reasoning_on),
    ):
        if event["type"] == "done":
            response = event.get("message") or {}
    return response
