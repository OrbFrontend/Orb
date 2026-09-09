"""Bind format and voice normalization to the post-pipeline hook."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..toolkit import (
    EV_DRAFT_REPLACED,
    forced_tool_call,
    get_workflow_config,
    normalize_to_baseline,
)
from . import WORKFLOW_ID, normalize_config
from .voice import classifier_ready, classify, drift, labels_for, target

logger = logging.getLogger(__name__)

BASELINE_WINDOW = 3

# What the rewrite must not touch. The editor may have just rewritten this draft
# for the length guard, so length is named alongside the usual preservation rules:
# a voice fix that grows the reply would silently undo that pass.
_INSTRUCTION = (
    "[OOC: The assistant message above drifted out of the narrative voice this "
    "scene has been written in. Call `editor_rewrite` to restate it in {voice}.\n\n"
    "REWRITING RULES:\n"
    "- Change ONLY the narrative voice. Keep every story beat, every line of "
    "dialogue, the author's vocabulary, and all formatting exactly as they are.\n"
    "- Keep the rewrite the same length as the draft. Do not add, expand, or trim.\n"
    "- Rewrite the whole draft, not an excerpt.]"
)


def _baseline_window(history) -> list[Mapping[str, Any]]:
    """The recent assistant-message rows (newest first, up to 3) whose conventions
    the draft is held to.

    Mirrors the fallback window the editor pass derived from its cached prefix:
    assistant history is always plain text, so a non-str body (the multimodal
    list form rides only user messages) has nothing to contribute. Rows rather
    than bare content because the voice half caches its labels per message id.
    """
    window: list[Mapping[str, Any]] = []
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                window.append(msg)
                if len(window) >= BASELINE_WINDOW:
                    break
    return window


async def _voice_enabled(ctx) -> bool:
    """Whether the voice half runs this turn.

    The free precondition is asked first: ``classifier_ready`` reads the settings
    mapping the ctx already carries, so a host without the POV/tense model never
    pays the config slot's round trip.
    """
    if not classifier_ready(ctx.settings):
        return False
    return normalize_config(await get_workflow_config(WORKFLOW_ID))["voice_consistency"]


async def _voice_rewrite(ctx, text: str, phrases: list[str]) -> str:
    """Ask the Editor to restate *text* in the baseline voice; "" on any failure.

    The tail mirrors the editor's own ``trailing`` so on the agent lane this call
    extends the prefix the editor just warmed this turn, and the per-call
    instruction rides behind a byte-identical shared prefix rather than entering it.
    Known divergence: the editor replays ``state.writer_content``, which may be a
    multimodal list; ``PostCtx`` carries only ``effective_msg``, so on attachment
    turns reuse degrades to the shared prefix.
    """
    instruction = _INSTRUCTION.format(voice=" and ".join(phrases))
    args: dict = {}
    async for event in forced_tool_call(
        client=ctx.agent_client or ctx.client,
        prefix=ctx.agent_prefix or ctx.prefix,
        tail_messages=[
            {"role": "user", "content": ctx.effective_msg},
            {"role": "assistant", "content": text},
            {"role": "user", "content": instruction},
        ],
        tool_name="editor_rewrite",
        settings=ctx.settings,
        model_name=ctx.agent_model_name or None,
        # Unchanged, not overlaid: this renders the pipeline's own byte-identical
        # tool blob, and appends the canonical editor_rewrite schema only when the
        # length guard has not already put it there.
        enabled_tools=ctx.enabled_tools,
        schema_overrides=ctx.schema_overrides,
        kv_tracker=ctx.kv_tracker,
        reasoning_on=False,
        temperature=0.25,
        token_floor=8192,
    ):
        if event.get("type") == "result" and isinstance(event.get("args"), dict):
            args = event["args"]
    rewritten = args.get("rewritten_text")
    return rewritten if isinstance(rewritten, str) else ""


async def _hold_voice(ctx, text: str, window: list[Mapping[str, Any]]) -> str:
    """The draft restated in the window's voice, or *text* unchanged.

    An ambiguous end, an empty rewrite, or a classifier that answered the sentinel
    all return *text*; anything that raises is caught by the caller.
    """
    baseline = target([await labels_for(msg) for msg in window])
    if baseline == ("ambiguous", "ambiguous"):
        return text
    source = await classify(text)
    # An empty or all-dialogue draft shapes to "" and classifies ambiguous, so it
    # falls out here by construction rather than by a length check.
    phrases = drift(source, baseline)
    if not phrases:
        return text
    logger.info(
        "format-consistency: voice drift %s -> %s; requesting a rewrite in %s",
        source,
        baseline,
        ", ".join(phrases),
    )
    rewritten = await _voice_rewrite(ctx, text, phrases)
    return rewritten if rewritten and rewritten != text else text


async def post_pipeline(ctx):
    """Hold the finished draft's markup convention -- and optionally its narrative
    voice -- to the recent messages'.

    Suspension is the framework's job: the per-workflow toggle gates this hook in
    the fan-out loop, so when reached the hook always runs. The two repairs
    compose before yielding, because the hook has a one-``draft_replaced`` budget
    (the bridge warns and drops a second).
    """
    window = _baseline_window(ctx.history)
    text = ctx.draft

    # One guard over the whole voice half. Markup normalization is this workflow's
    # always-on job; the voice check is opt-in and reaches a local model, the
    # config slot, the message-state cache and an LLM endpoint -- four things that
    # can fail independently. Any of them costs the check, never the markup fix.
    # Nothing in here raises WorkflowUserFacingError: that is for a
    # misconfiguration the user chose, not for a flaky call.
    try:
        if await _voice_enabled(ctx):
            text = await _hold_voice(ctx, text, window)
    except Exception:
        logger.exception("format-consistency: voice check failed; normalizing markup only")
        text = ctx.draft

    # Always last: an LLM rewrite reintroduces markup drift, and this algorithmic
    # pass is the cheap authority on that.
    # The pure normalizer keeps an on/off param for its own test surface; the real
    # gate is the framework toggle, so this path always passes True.
    baseline_msgs = [msg.get("content", "") for msg in window]
    text, report = normalize_to_baseline(text, baseline_msgs, enabled=True)
    if report.changed:
        logger.info("format-consistency: normalized draft (%s)", report.transition())
    if text != ctx.draft:
        yield {"type": EV_DRAFT_REPLACED, "draft": text}
