"""Workflow binding for deterministic format normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..toolkit import ToolSpec, Workflow

WORKFLOW_ID = "format_consistency"
VOICE_REWRITE_TOOL_NAME = "voice_rewrite"
VOICE_REWRITE_LENGTH_RULE = "Keep the rewrite the same length as the passage. Do not add, expand, or trim."

VOICE_REWRITE_TOOL = ToolSpec(
    name=VOICE_REWRITE_TOOL_NAME,
    schema={
        "type": "function",
        "function": {
            "name": VOICE_REWRITE_TOOL_NAME,
            "description": (
                "Restate the entire passage in the requested narrative voice and change nothing else. "
                f"{VOICE_REWRITE_LENGTH_RULE} Preserve every story beat, line of dialogue, the author's vocabulary, "
                "and all formatting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rewritten_text": {
                        "type": "string",
                        "description": "The entire passage restated only in the requested narrative voice.",
                    }
                },
                "required": ["rewritten_text"],
            },
        },
    },
    choice={"type": "function", "function": {"name": VOICE_REWRITE_TOOL_NAME}},
)

# Markup follows the workflow toggle; voice enforcement is opt-in because it
# costs an LLM call and requires the local classifier.
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
    """Return the normalized UI and hook configuration."""
    raw = raw if isinstance(raw, Mapping) else {}
    voice_consistency = raw.get("voice_consistency")
    return {
        "voice_consistency": (
            voice_consistency if isinstance(voice_consistency, bool) else CONFIG_DEFAULTS["voice_consistency"]
        )
    }


format_consistency_workflow = Workflow(
    id=WORKFLOW_ID,
    display_name="Format Consistency",
    tools=[VOICE_REWRITE_TOOL],
    config_schema=CONFIG_SCHEMA,
    config_defaults=CONFIG_DEFAULTS,
    config_normalizer=normalize_config,
)
