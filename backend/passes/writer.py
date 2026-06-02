"""
passes/writer.py — Writer pass: the main phase that streams the story response.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, List, Mapping, Optional

from ..llm_client import LLMClient, reasoning_cfg
from ..tool_defs import enabled_schemas
from ..utils import extract_hyperparams, build_multimodal_content

logger = logging.getLogger(__name__)


def build_writer_content(
    lorebook_block: str,
    inj_block: str,
    enabled_tools: dict,
    effective_msg: str,
    attachments: list[dict] | None,
    length_guard_enforce: bool,
    length_guard: dict | None,
) -> "str | list":
    """Build the writer's user-message content (string or multimodal list).

    Extracted so the orchestrator can pass the exact value to the editor,
    letting it replicate the writer's last user message for KV-cache reuse.
    """
    tail = ""
    if lorebook_block:
        tail += "___\n\n" + lorebook_block + "\n\n"
    if inj_block:
        tail += "___\n\n" + inj_block + "\n\n"
    if enabled_tools:
        tail += "**Do not use tool or function calls this turn.**\n\n"
    if length_guard_enforce and length_guard and length_guard.get("enabled"):
        max_words = length_guard.get("max_words", 240)
        max_paragraphs = length_guard.get("max_paragraphs", 4)
        tail += f"**Keep your response under {max_words} words and {max_paragraphs} paragraphs.**\n\n"
    tail += "___\n\n" + effective_msg + "\n\n"

    return build_multimodal_content(tail, attachments)


async def _writer_pass(
    client: LLMClient,
    prefix: list[dict],
    settings: Mapping[str, Any],
    enabled_tools: dict,
    *,
    inj_block: str = "",
    lorebook_block: str = "",
    effective_msg: str,
    attachments: Optional[List[dict]] = None,
    length_guard_enforce: bool = False,
    length_guard: dict | None = None,
    kv_tracker=None,
    reasoning_on: bool = True,
    schema_overrides: dict | None = None,
) -> AsyncIterator[dict]:
    """Yields {"type": "content"|"reasoning", "delta": str} dicts."""
    content = build_writer_content(
        lorebook_block,
        inj_block,
        enabled_tools,
        effective_msg,
        attachments,
        length_guard_enforce,
        length_guard,
    )

    msgs = prefix + [{"role": "user", "content": content}]

    hyperparams = extract_hyperparams(settings)
    schemas = enabled_schemas(enabled_tools, schema_overrides)
    logger.info(
        "Writer pass: tools included=%s",
        json.dumps([s["function"]["name"] for s in schemas]) if schemas else "[]",
    )
    extra: dict = {"tools": schemas, "tool_choice": "none"} if schemas else {}
    extra.update(reasoning_cfg(reasoning_on))

    if kv_tracker is not None:
        kv_tracker.record("writer", msgs, schemas if schemas else None, model=settings["model_name"])

    async for item in client.complete(messages=msgs, model=settings["model_name"], **extra, **hyperparams):
        if item["type"] == "done":
            if kv_tracker is not None:
                kv_tracker.record_usage("writer", item.get("usage"))
            return
        yield item
