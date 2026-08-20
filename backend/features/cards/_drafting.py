"""The shape both card drafters share: one forced tool call, drained and parsed.

``public_profile`` and ``sheet_update`` are siblings — same forced-call posture,
same hardcoded summarization hyperparameters, same "``LLMCallError`` propagates
untouched" rule, same brace check on the way out. This is that shape, written
once, so the two cannot answer differently about what a usable reply is.

Deliberately *not* ``workflows._forced_call.forced_tool_call``: that helper
resolves its schema out of ``inference.tool_registry.TOOLS``, and neither of
these tools is registered there — a profile and a sheet update are bookkeeping
about a scene rather than a phase of a turn, so forcing them into that module's
turn-phase partition would be the wrong statement. What is shared here is the
drain, not the registry.
"""

from __future__ import annotations

import re
from typing import Any

from ...core import ChatMessage
from ...inference import LLMClient, parse_tool_calls

_WHITESPACE_RE = re.compile(r"\s+")

# A drafted field is macro-resolved at turn time (``inference/group_context``),
# so a generated brace would quietly substitute months later, inside a string
# the user already reviewed and approved.
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
