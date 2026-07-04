"""
passes/writer.py — The writer pass: streams the main story response.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Sequence

from ...core import (
    ChatMessage,
    ContentPart,
    build_multimodal_content,
    extract_hyperparams,
)
from ...inference import CachedBase, LLMClient, _KVCacheTracker, reasoning_cfg
from .editor.length_guard import LengthGuard, writer_nudge

if TYPE_CHECKING:
    from ..state import TurnState, _PipelineConfig

logger = logging.getLogger(__name__)


def build_writer_content(
    lorebook_block: str,
    inj_block: str,
    enabled_tools: Mapping[str, bool],
    effective_msg: str,
    attachments: Sequence[Mapping[str, Any]] | None,
    length_guard: LengthGuard | None,
    text_mode: bool = False,
) -> "str | list[ContentPart]":
    """Build the writer's user-message content (string or multimodal list).

    Built once and threaded into both the writer pass and the editor, which
    replays it verbatim to extend the writer's KV-cached prefix. The length-guard
    nudge (preventive arm) fires only in enforce mode; a non-None *length_guard*
    already means the feature is enabled. In *text_mode* the no-tools nudge is
    dropped — no tool harness is rendered, so the instruction is meaningless.
    """
    tail = ""
    if lorebook_block:
        tail += "___\n\n" + lorebook_block + "\n\n"
    if inj_block:
        tail += "___\n\n" + inj_block + "\n\n"
    if enabled_tools and not text_mode:
        tail += "**Do not use tool or function calls this turn.**\n\n"
    tail += writer_nudge(length_guard)
    tail += "___\n\n" + effective_msg + "\n\n"

    return build_multimodal_content(tail, attachments)


async def writer_pass(
    client: LLMClient,
    base: CachedBase,
    settings: Mapping[str, Any],
    content: "str | list[ContentPart]",
    *,
    kv_tracker=None,
    reasoning_on: bool = True,
) -> AsyncIterator[dict]:
    """Yield ``{"type": "content"|"reasoning", "delta": str}`` dicts.

    *content* is the writer's user-message body, prebuilt by
    ``build_writer_content`` and shared with the editor. The tool blob comes from
    *base* so it stays byte-identical with the director and editor passes.
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


async def writer_stage(
    cfg: "_PipelineConfig",
    state: "TurnState",
    *,
    settings: Mapping[str, Any],
    attachments: Sequence[Mapping[str, Any]],
    kv_tracker: _KVCacheTracker,
) -> AsyncIterator[dict]:
    """Input-prep + writer pass + event translation.

    Builds ``state.writer_content`` once (replayed verbatim by the editor to
    extend the writer's KV-cached prefix), runs :func:`writer_pass` translating
    ``content``→``token`` and ``reasoning``→``reasoning`` events, and accumulates
    the writer's wall time into ``state.latency``.
    """
    state.writer_content = build_writer_content(
        state.writer_lorebook_block,
        state.inj_block,
        cfg.writer_enabled_tools,
        state.effective_msg,
        attachments,
        cfg.length_guard,
        cfg.writer_text_mode,
    )
    writer_t0 = time.monotonic()
    async for item in writer_pass(
        cfg.writer_lane.client,
        cfg.writer_lane.base,
        settings,
        state.writer_content,
        kv_tracker=kv_tracker,
        reasoning_on=cfg.writer_reasoning_on,
    ):
        if item["type"] == "reasoning":
            state.reasoning_writer += item["delta"]
            yield {
                "event": "reasoning",
                "data": {"pass": "writer", "delta": item["delta"]},
            }
        else:
            state.resp_text += item["delta"]
            yield {"event": "token", "data": item["delta"]}
    # agent_latency_ms is the whole turn's wall time; accumulate the writer's
    # span here (director + editor add their own).
    state.latency += int((time.monotonic() - writer_t0) * 1000)
