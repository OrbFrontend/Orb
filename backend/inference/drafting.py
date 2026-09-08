"""One-shot forced tool call, drained and parsed.

Lives here rather than in the slice that first needed it: draining a forced
call and parsing its arguments *is* model execution, and the consumers are now
peers (``features/cards`` drafts profiles and sheets, ``features/library_tags``
tags the library). A slice may not import a peer slice, so the lowest layer all
of them reach is the only home that does not fork this into a third copy.

Not to be confused with ``workflows._forced_call.forced_tool_call``, which is
the *other* forced-call helper and knows strictly more: it consults
``honors_forced_tool_choice`` and demotes an endpoint that ignores the field.
That knowledge is about collapsing a multi-tool array down to the forced one,
and callers here ship a single-tool array where there is nothing to collapse --
a provider that ignores ``tool_choice`` and answers in prose is reported as the
caller's own ``*Unavailable`` instead. Reach for that one when a call has to
ride a pipeline's shared tools blob; this one when it is a schema and a budget.
"""

from __future__ import annotations

import re
from typing import Any

from ..core import ChatMessage
from .client import LLMClient, parse_tool_calls, reasoning_cfg

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
    reasoning_on: bool,
) -> dict[str, Any] | None:
    """One forced call to *tool*, drained. Returns its arguments, or ``None``.

    ``None`` means the endpoint answered without calling the tool it was given;
    each caller turns that into its own ``*Unavailable`` sentence, because the
    two describe different things to the user.

    Hyperparameters are hardcoded and deliberately do *not* go through
    ``core.extract_hyperparams``: that path exists for prose the user asked to
    be rewritten and wants their writing preset applied to, while a roleplay
    preset at ``temperature: 1.15`` would embellish a summarization call.

    *reasoning_on* is required, and has no "leave it alone" value on purpose.
    Sending no reasoning params is *not* the same as sending "off": both
    transports read an absent hint as thinking ON (``text_completion``'s
    ``reasoning_enabled`` says so in as many words, and a chat template's default
    render does the same). So a caller that simply never mentioned reasoning was
    silently opting into it -- and reasoning is spent from the very
    ``max_tokens`` the answer needs, so a budget consumed thinking returns no
    tool call at all, which each caller reports as its own ``*Unavailable``.
    Every forced call here is a schema to fill, on a budget sized for the answer;
    making the decision unskippable is what keeps the next call site from
    inheriting that bug by omission.
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
        **reasoning_cfg(reasoning_on),
    ):
        if event.get("type") == "done":
            response = event.get("message") or {}
    return next((call.get("arguments") or {} for call in parse_tool_calls(response) if call.get("name") == name), None)
