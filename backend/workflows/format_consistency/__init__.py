"""Workflow binding for deterministic format normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..toolkit import Workflow

WORKFLOW_ID = "format_consistency"

# One boolean, so it is declared here rather than in a config.py module of its
# own (the same call image_gen's declaration makes for a much larger schema).
# The markup half needs no config: the framework's per-workflow toggle is its
# only switch. Voice enforcement is opt-in because it costs an LLM call per
# drifting turn and needs a local model the user has to download first.
CONFIG_DEFAULTS: dict[str, Any] = {"voice_consistency": False}

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "voice_consistency": {
            "type": "boolean",
            "title": "Keep POV and tense to recent messages (needs the POV/tense model)",
        }
    },
}


def normalize_config(raw: Mapping[str, Any] | None) -> dict:
    """Coerce the stored slot to the declared shape.

    A PUT can write anything, and the hook branches on this value, so the truthy
    coercion happens once here instead of at the read.
    """
    raw = raw if isinstance(raw, Mapping) else {}
    return {"voice_consistency": bool(raw.get("voice_consistency", CONFIG_DEFAULTS["voice_consistency"]))}


format_consistency_workflow = Workflow(
    id=WORKFLOW_ID,
    display_name="Format Consistency",
    config_schema=CONFIG_SCHEMA,
    config_defaults=CONFIG_DEFAULTS,
    config_normalizer=normalize_config,
)
