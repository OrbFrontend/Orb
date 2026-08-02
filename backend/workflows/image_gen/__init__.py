"""External-ComfyUI image generation secondary workflow declaration."""

from __future__ import annotations

from ..registry import Workflow
from .config import CONFIG_DEFAULTS, SOURCES, normalize_config
from .pov import POV_MODES
from .prompts import ANALYZE_TOOL, COMPOSE_TOOL

_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "enum": list(SOURCES),
            "title": "Image backend",
        },
        "default_style": {"type": "string", "title": "Default style"},
        "styles": {"type": "array", "title": "Styles"},
        "pov_mode": {"type": "string", "enum": list(POV_MODES), "title": "Camera"},
        "scene_analysis": {"type": "boolean", "title": "Analyze complex scenes"},
        "prompter_reasoning": {"type": "boolean", "title": "Enable prompter thinking"},
        "timeout_seconds": {
            "type": "number",
            "minimum": 10,
            "maximum": 900,
            "title": "Render timeout",
        },
        "external_comfy": {"type": "object", "title": "External ComfyUI"},
        "cloud": {"type": "object", "title": "Cloud API"},
    },
}

image_gen_workflow = Workflow(
    id="image_gen",
    display_name="Image Generation",
    produces_artifacts=True,
    tools=[COMPOSE_TOOL, ANALYZE_TOOL],
    config_schema=_CONFIG_SCHEMA,
    config_defaults=CONFIG_DEFAULTS,
    # The config carries user-authored graphs and style entries that
    # `normalize_config` bounds and drops; without this the settings panel would
    # keep listing a workflow the render path silently ignores.
    config_normalizer=normalize_config,
)
