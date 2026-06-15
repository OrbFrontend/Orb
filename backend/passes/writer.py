"""
passes/writer.py — Writer pass: the main phase that streams the story response.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Sequence

from ..cached_call import CachedBase
from ..kv_tracker import _KVCacheTracker
from ..llm_client import LLMClient, reasoning_cfg
from ..core import ChatMessage, ContentPart
from ..core import build_multimodal_content, extract_hyperparams
from .editor.length_guard import LengthGuard, writer_nudge

if TYPE_CHECKING:
    from ..pipeline_state import TurnState, _PipelineConfig

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


async def writer_stage(
    cfg: "_PipelineConfig",
    state: "TurnState",
    *,
    settings: Mapping[str, Any],
    attachments: Sequence[Mapping[str, Any]],
    kv_tracker: _KVCacheTracker,
) -> AsyncIterator[dict]:
    """Writer stage: input-prep + writer pass + event translation, owned here so
    the orchestrator sequences passes rather than threading writer internals.

    Builds ``state.writer_content`` once (threaded into the writer pass and later
    replayed verbatim by the editor to extend the writer's KV-cached prefix),
    runs :func:`writer_pass` translating ``content``→``token`` /
    ``reasoning``→``reasoning`` SSE events, and folds the writer's wall time into
    ``state.latency``. The writer pass only streams tokens and reports no
    duration of its own, so the timing is taken here.
    """
    state.writer_content = build_writer_content(
        state.writer_lorebook_block,
        state.inj_block,
        cfg.writer_enabled_tools,
        state.effective_msg,
        attachments,
        cfg.length_guard,
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
