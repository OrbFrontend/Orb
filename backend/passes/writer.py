"""
passes/writer.py — Writer pass: streams the story response with tool-call
token suppression via logit bias.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from ..llm_client import LLMClient
from ..tool_defs import enabled_schemas

logger = logging.getLogger(__name__)


# Token strings that signal the start of a tool-call block.  If logit_bias
# suppression fails (wrong ID, unsupported by backend, etc.) and one of these
# leaks into the writer's output stream, we truncate immediately so the user
# never sees the raw markup.
_WRITER_LEAK_MARKERS = {
    "<|tool_call>",
    "<|python_tag|>",
    "[TOOL_CALL]",
    "<tool_call>",
    "<function_calls>",
    "<|tool_calls|>",
    "<|function_calls|>",
}


async def _writer_pass(
    client: LLMClient, msgs: list[dict], settings: dict,
    enabled_tools: dict | None = None,
    tool_start_token_id: int | None = None,
) -> AsyncIterator[dict]:
    """Yields {"type": "content"|"reasoning", "delta": str} dicts."""
    params = {
        k: v for k in ["temperature", "max_tokens", "top_p", "min_p", "top_k", "repetition_penalty"]
        if (v := settings.get(k)) is not None
    }
    schemas = enabled_schemas(enabled_tools)
    # Only include tool schemas when we have a confirmed suppression token.
    # Without logit_bias, small models ignore tool_choice:"none" and emit
    # tool-call tokens anyway, causing hallucinated output.
    if schemas and tool_start_token_id is None:
        logger.info("Writer pass: skipping tools (no suppression token discovered) to prevent hallucination")
        schemas = []
    logger.info(
        "Writer pass: tools included=%s",
        json.dumps([s["function"]["name"] for s in schemas]) if schemas else "[]",
    )
    extra: dict = {"tools": schemas, "tool_choice": "none"} if schemas else {}
    extra["reasoning"] = {"effort": "low", "enabled": True}
    if tool_start_token_id is not None:
        extra["logit_bias"] = {tool_start_token_id: -100}
        logger.info("Writer pass: logit_bias {%d: -100} applied", tool_start_token_id)

    # Rolling tail buffer: most control tokens arrive as a single delta, but
    # we keep the last 50 chars to catch any that straddle a token boundary.
    tail = ""
    async for item in client.stream(messages=msgs, model=settings["model_name"], **extra, **params):
        if item["type"] == "content":
            tail = (tail + item["delta"])[-50:]
            for marker in _WRITER_LEAK_MARKERS:
                if marker in tail:
                    logger.warning(
                        "Writer pass: tool-call marker '%s' leaked through suppression — truncating output",
                        marker,
                    )
                    return
        yield item
