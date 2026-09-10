"""Bind format and voice normalization to the post-pipeline hook."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..toolkit import (
    EV_DRAFT_REPLACED,
    baseline_axes,
    forced_tool_call,
    get_workflow_config,
    local_feature_ready,
    normalize_to_baseline,
)
from . import (
    VOICE_REWRITE_LENGTH_RULE,
    VOICE_REWRITE_TOOL_NAME,
    WORKFLOW_ID,
    normalize_config,
)
from .guard import rejection
from .voice import (
    FEATURE,
    UNKNOWN_LABELS,
    VoiceLabels,
    classify,
    drift,
    labels_for,
    target,
)

logger = logging.getLogger(__name__)

BASELINE_WINDOW = 3

# Keep the voice rewrite on a short, constant-prefix lane: it only needs the draft,
# and must not undo the Editor's length guard.
_SYSTEM = (
    "You are a copy editor. You restate a passage of prose in a different narrative "
    "voice and change nothing else.\n\n"
    "RULES:\n"
    "- Change ONLY the narrative voice. Keep every story beat, every line of "
    "dialogue, the author's vocabulary, and all formatting exactly as they are.\n"
    f"- {VOICE_REWRITE_LENGTH_RULE}\n"
    "- Restate the whole passage, not an excerpt.\n"
    "- Pronouns and names already in the passage keep their referents. Do not "
    "introduce a character, a name, or a detail the passage does not contain."
)

_INSTRUCTION = (
    f"Restate the passage below in {{voice}}. Call `{VOICE_REWRITE_TOOL_NAME}` with the result.\n\nPASSAGE:\n{{draft}}"
)


def _baseline_window(history) -> list[Mapping[str, Any]]:
    """Return up to three recent plain-text assistant rows, newest first."""
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
    """Whether the local classifier and opt-in voice check are enabled."""
    if not local_feature_ready(FEATURE, ctx.settings):
        return False
    return normalize_config(await get_workflow_config(WORKFLOW_ID))["voice_consistency"]


async def _voice_rewrite(ctx, text: str, phrases: list[str]) -> str:
    """Restate *text* on a self-contained voice-rewrite lane."""
    args: dict = {}
    async for event in forced_tool_call(
        client=ctx.agent_client or ctx.client,
        prefix=[{"role": "system", "content": _SYSTEM}],
        tail_messages=[
            {
                "role": "user",
                "content": _INSTRUCTION.format(voice=" and ".join(phrases), draft=text),
            }
        ],
        tool_name=VOICE_REWRITE_TOOL_NAME,
        settings=ctx.settings,
        model_name=ctx.agent_model_name or None,
        # None makes forced_tool_call send only the forced tool.
        enabled_tools=None,
        kv_tracker=ctx.kv_tracker,
        cache_shape="format_consistency:voice_rewrite",
        reasoning_on=False,
        temperature=0.25,
        token_floor=8192,
    ):
        if event.get("type") == "result" and isinstance(event.get("args"), dict):
            args = event["args"]
    rewritten = args.get("rewritten_text")
    return rewritten if isinstance(rewritten, str) else ""


async def _hold_voice(ctx, text: str, window: list[Mapping[str, Any]]) -> str:
    """Return *text* in the window's voice, or unchanged when it is ambiguous."""
    window_labels: list[VoiceLabels] = []
    for msg in window:
        labels = await labels_for(msg)
        if labels is None:
            return text
        window_labels.append(labels)
    baseline = target(window_labels)
    if baseline == UNKNOWN_LABELS:
        return text
    source = await classify(text)
    if source is None:
        return text
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
    if not rewritten:
        return text
    reason = rejection(text, rewritten)
    if reason:
        logger.info("format-consistency: discarding the voice rewrite (%s)", reason)
        return text
    return rewritten


async def post_pipeline(ctx):
    """Normalize the finished draft's markup and, optionally, its narrative voice."""
    window = _baseline_window(ctx.history)
    baseline_msgs = [msg.get("content", "") for msg in window]
    convention = baseline_axes(baseline_msgs)
    text = ctx.draft

    # Voice failures must not prevent the always-on markup normalization.
    try:
        if await _voice_enabled(ctx):
            text = await _hold_voice(ctx, text, window)
    except Exception:
        logger.exception("format-consistency: voice check failed; normalizing markup only")
        text = ctx.draft

    # Run markup normalization last because the voice rewrite can reintroduce drift.
    text, report = normalize_to_baseline(text, baseline_msgs, enabled=True, target=convention)
    if report.changed:
        logger.info("format-consistency: normalized draft (%s)", report.transition())
    if text != ctx.draft:
        yield {"type": EV_DRAFT_REPLACED, "draft": text}
