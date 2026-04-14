"""
passes/writer.py — Writer pass: streams the story response with tool-call
token suppression via logit bias.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from ..llm_client import LLMClient, reasoning_cfg
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
    client: LLMClient,
    prefix: list[dict],
    settings: dict,
    enabled_tools: dict | None = None,
    tool_start_token_id: int | None = None,
    *,
    inj_block: str = "",
    effective_msg: str,
    length_guard_enforce: bool = False,
    length_guard: dict | None = None,
    kv_tracker=None,
    reasoning_on: bool = True,
) -> AsyncIterator[dict]:
    """Yields {"type": "content"|"reasoning", "delta": str} dicts."""
    tail = ""
    if inj_block:
        tail += "___\n\n" + inj_block + "\n\n"
    if len(enabled_tools) > 0:
        tail += "**Do not use tool or function calls.**\n\n"
    if length_guard_enforce and length_guard and length_guard.get("enabled"):
        max_words = length_guard.get("max_words", 240)
        max_paragraphs = length_guard.get("max_paragraphs", 4)
        tail += f"**Keep your response under {max_words} words and {max_paragraphs} paragraphs.**\n\n"
    tail += "___\n\n" + effective_msg + "\n\n"

    msgs = prefix + [{"role": "user", "content": tail}]

    params = {
        k: v for k in ["temperature", "max_tokens", "top_p", "min_p", "top_k", "repetition_penalty"]
        if (v := settings.get(k)) is not None
    }
    schemas = enabled_schemas(enabled_tools)
    # Only include tool schemas when we have a confirmed suppression token.
    # Without logit_bias, small models ignore tool_choice:"none" and emit tool-call tokens anyway, causing hallucinated output.
    if schemas and tool_start_token_id is None:
        logger.info("Writer pass: skipping tools (no suppression token discovered) to prevent hallucination")
        schemas = []
    logger.info(
        "Writer pass: tools included=%s",
        json.dumps([s["function"]["name"] for s in schemas]) if schemas else "[]",
    )
    extra: dict = {"tools": schemas, "tool_choice": "none"} if schemas else {}
    extra.update(reasoning_cfg(reasoning_on))
    if tool_start_token_id is not None:
        extra["logit_bias"] = {tool_start_token_id: -100}
        logger.info("Writer pass: logit_bias {%d: -100} applied", tool_start_token_id)

    if kv_tracker is not None:
        kv_tracker.record("writer", msgs, schemas if schemas else None)

    # Rolling tail buffer: most control tokens arrive as a single delta, but we keep the last 50 chars to catch any that straddle a token boundary.
    tail = ""
    async for item in client.complete(messages=msgs, model=settings["model_name"], **extra, **params):
        if item["type"] == "done":
            return
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
