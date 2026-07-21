"""External-ComfyUI image generation secondary workflow declaration."""

from __future__ import annotations

from ..registry import Workflow
from .composer import COMPOSE_TOOL
from .config import CONFIG_DEFAULTS, normalize_config

_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "enum": ["external_comfy"],
            "title": "Image backend",
        },
        "default_style": {"type": "string", "title": "Default style"},
        "scene_analysis": {"type": "boolean", "title": "Analyze complex scenes"},
        "timeout_seconds": {
            "type": "number",
            "minimum": 10,
            "maximum": 900,
            "title": "Render timeout",
        },
        "external_comfy": {"type": "object", "title": "External ComfyUI"},
    },
}

image_gen_workflow = Workflow(
    id="image_gen",
    display_name="Image Generation",
    produces_artifacts=True,
    tools=[COMPOSE_TOOL],
    config_schema=_CONFIG_SCHEMA,
    config_defaults=CONFIG_DEFAULTS,
    # The config carries user-authored graphs and style entries that
    # `normalize_config` bounds and drops; without this the settings panel would
    # keep listing a workflow the render path silently ignores.
    config_normalizer=normalize_config,
)
