"""Bind format normalization to the post-pipeline hook."""

from __future__ import annotations

import logging

from ..toolkit import EV_DRAFT_REPLACED, normalize_to_baseline

logger = logging.getLogger(__name__)

BASELINE_WINDOW = 3


def _baseline_window(history) -> list[str]:
    """The recent assistant-message window (newest first, up to 3) whose markup
    convention the draft is held to.

    Mirrors the fallback window the editor pass derived from its cached prefix:
    assistant history is always plain text, so a non-str body (the multimodal
    list form rides only user messages) has nothing to contribute.
    """
    window: list[str] = []
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                window.append(content)
                if len(window) >= BASELINE_WINDOW:
                    break
    return window


async def post_pipeline(ctx):
    """Hold the finished draft's markup convention to the recent messages'.

    Suspension is the framework's job: the per-workflow toggle gates this hook in
    the fan-out loop, so when reached the hook always runs.
    ``normalize_to_baseline`` is a conservative no-op -- it rewrites only when the
    baseline window agrees on a convention and the draft drifts from it -- so when
    nothing changes the hook yields nothing (no malformed-event warning).
    """
    baseline_msgs = _baseline_window(ctx.history)
    # The pure normalizer keeps an on/off param for its own test surface; the real
    # gate is the framework toggle, so this path always passes True.
    new_text, drift = normalize_to_baseline(ctx.draft, baseline_msgs, enabled=True)
    if drift.changed:
        logger.info("format-consistency: normalized draft (%s)", drift.transition())
        yield {"type": EV_DRAFT_REPLACED, "draft": new_text}
