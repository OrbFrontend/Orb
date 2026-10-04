"""Workflow declaration for deterministic format normalization."""

from __future__ import annotations

from ..toolkit import HookType, Workflow, subscription
from . import hooks
from .config import (
    CONFIG_DEFAULTS,
    CONFIG_SCHEMA,
    VOICE_REWRITE_LENGTH_RULE,
    VOICE_REWRITE_TOOL,
    VOICE_REWRITE_TOOL_NAME,
    WORKFLOW_ID,
    normalize_config,
)

WORKFLOW = Workflow(
    id=WORKFLOW_ID,
    display_name="Format Consistency",
    tools=[VOICE_REWRITE_TOOL],
    config_schema=CONFIG_SCHEMA,
    config_defaults=CONFIG_DEFAULTS,
    config_normalizer=normalize_config,
    # Negative priority makes the deterministic markup normalizer run before TTS's
    # post hook (priority 0), so TTS — and any future artifact hook — synthesizes
    # from the normalized text rather than the raw draft.
    subscriptions=[subscription(HookType.POST_PIPELINE, hooks.post_pipeline, priority=-10)],
)

__all__ = [
    "VOICE_REWRITE_LENGTH_RULE",
    "VOICE_REWRITE_TOOL",
    "VOICE_REWRITE_TOOL_NAME",
    "WORKFLOW",
    "WORKFLOW_ID",
    "normalize_config",
]
