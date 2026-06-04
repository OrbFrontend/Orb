"""
passes/writer.py — Writer pass: the main phase that streams the story response.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Mapping, Sequence

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
    length_guard: LengthGuard | None,
) -> "str | list[ContentPart]":
    """Build the writer's user-message content (string or multimodal list).

    Built once by the orchestrator and threaded into both the writer pass and
    the editor, which replays it verbatim to reuse the writer's KV-cached prefix.
    The length-guard nudge is the *preventive* arm: it fires only in enforce mode
    (``length_guard["enforce"]``); a non-None ``length_guard`` already means the
    feature is enabled.
    """
    tail = ""
    if lorebook_block:
        tail += "___\n\n" + lorebook_block + "\n\n"
    if inj_block:
        tail += "___\n\n" + inj_block + "\n\n"
    if enabled_tools:
        tail += "**Do not use tool or function calls this turn.**\n\n"
    if length_guard and length_guard["enforce"]:
        max_words = length_guard["max_words"]
        max_paragraphs = length_guard["max_paragraphs"]
        tail += f"**Keep your response under {max_words} words and {max_paragraphs} paragraphs.**\n\n"
    tail += "___\n\n" + effective_msg + "\n\n"

    return build_multimodal_content(tail, attachments)


async def _writer_pass(
    client: LLMClient,
    base: CachedBase,
    settings: Mapping[str, Any],
    content: "str | list[ContentPart]",
    *,
    kv_tracker=None,
    reasoning_on: bool = True,
) -> AsyncIterator[dict]:
    """Yields {"type": "content"|"reasoning", "delta": str} dicts.

    *content* is the writer's user-message body, prebuilt by the orchestrator via
    ``build_writer_content`` (and shared with the editor). The tool *schema* blob
    comes from ``base`` so it stays byte-identical with the director/editor passes.
    """
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
