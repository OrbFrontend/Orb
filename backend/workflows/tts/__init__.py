"""Text-to-speech workflow bindings."""

from __future__ import annotations

from ..toolkit import Workflow
from .config import CONFIG_DEFAULTS, CONFIG_SCHEMA, normalize_config

tts_workflow = Workflow(
    id="tts",
    display_name="Text-to-Speech",
    produces_artifacts=True,
    config_schema=CONFIG_SCHEMA,
    config_defaults=CONFIG_DEFAULTS,
    config_normalizer=normalize_config,
)
