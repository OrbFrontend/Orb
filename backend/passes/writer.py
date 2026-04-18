"""
passes/writer.py — Writer pass: streams the story response.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, List, Optional

from ..llm_client import LLMClient, reasoning_cfg
from ..tool_defs import enabled_schemas

logger = logging.getLogger(__name__)


async def _writer_pass(
    client: LLMClient,
    prefix: list[dict],
    settings: dict,
    enabled_tools: dict | None = None,
    *,
    inj_block: str = "",
    effective_msg: str,
    attachments: Optional[List[dict]] = None,
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

    # Build user message content, possibly multimodal
    if attachments:
        parts = [{"type": "text", "text": tail}]
        for att in attachments:
            mime = att.get("mime_type", att.get("mime", "image/jpeg"))
            b64 = att.get("data_b64", att.get("b64", ""))
            if not b64:
                continue
            url = f"data:{mime};base64,{b64}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
        content = parts
    else:
        content = tail

    msgs = prefix + [{"role": "user", "content": content}]

    params = {
        k: v
        for k in [
            "temperature",
            "max_tokens",
            "top_p",
            "min_p",
            "top_k",
            "repetition_penalty",
        ]
        if (v := settings.get(k)) is not None
    }
    schemas = enabled_schemas(enabled_tools)
    logger.info(
        "Writer pass: tools included=%s",
        json.dumps([s["function"]["name"] for s in schemas]) if schemas else "[]",
    )
    extra: dict = {"tools": schemas, "tool_choice": "none"} if schemas else {}
    extra.update(reasoning_cfg(reasoning_on))

    if kv_tracker is not None:
        kv_tracker.record("writer", msgs, schemas if schemas else None)

    async for item in client.complete(
        messages=msgs, model=settings["model_name"], **extra, **params
    ):
        if item["type"] == "done":
            return
        yield item
