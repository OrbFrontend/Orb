"""
passes/writer.py — Writer pass: the main phase that streams the story response.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from ..llm_client import LLMClient, reasoning_cfg
from ..kv_tracker import CachedBase
from ..llm_types import ChatMessage, ContentPart
from ..utils import LengthGuard, extract_hyperparams, build_multimodal_content

logger = logging.getLogger(__name__)


def build_writer_content(
    lorebook_block: str,
    inj_block: str,
    enabled_tools: Mapping[str, bool],
    effective_msg: str,
    attachments: Sequence[Mapping[str, Any]] | None,
    length_guard_enforce: bool,
    length_guard: LengthGuard | None,
) -> "str | list[ContentPart]":
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
    if length_guard_enforce and length_guard and length_guard["enabled"]:
        max_words = length_guard["max_words"]
        max_paragraphs = length_guard["max_paragraphs"]
        tail += f"**Keep your response under {max_words} words and {max_paragraphs} paragraphs.**\n\n"
    tail += "___\n\n" + effective_msg + "\n\n"

    return build_multimodal_content(tail, attachments)


async def _writer_pass(
    client: LLMClient,
    base: CachedBase,
    settings: Mapping[str, Any],
    enabled_tools: Mapping[str, bool],
    *,
    inj_block: str = "",
    lorebook_block: str = "",
    effective_msg: str,
    attachments: Optional[Sequence[Mapping[str, Any]]] = None,
    length_guard_enforce: bool = False,
    length_guard: LengthGuard | None = None,
    kv_tracker=None,
    reasoning_on: bool = True,
) -> AsyncIterator[dict]:
    """Yields {"type": "content"|"reasoning", "delta": str} dicts.

    *enabled_tools* still drives the in-prompt "do not use tools" notice; the
    tool *schema* blob comes from ``base`` (built from the same enabled-tool set)
    so it stays byte-identical with the director/editor passes.
    """
    content = build_writer_content(
        lorebook_block,
        inj_block,
        enabled_tools,
        effective_msg,
        attachments,
        length_guard_enforce,
        length_guard,
    )

    trailing: list[ChatMessage] = [{"role": "user", "content": content}]

    hyperparams = extract_hyperparams(settings)
    logger.info(
        "Writer pass: tools included=%s",
        json.dumps([t["function"]["name"] for t in base.tools]) if base.tools else "[]",
    )

    async for item in base.complete(
        client,
        label="writer",
        trailing=trailing,
        # base.tools is empty in dual-model (Invariant 5) → no tools, no
        # tool_choice; otherwise the writer ships the shared blob but is barred
        # from calling anything.
        tool_choice="none" if base.tools else None,
        kv_tracker=kv_tracker,
        **reasoning_cfg(reasoning_on),
        **hyperparams,
    ):
        if item["type"] == "done":
            return
        yield item
