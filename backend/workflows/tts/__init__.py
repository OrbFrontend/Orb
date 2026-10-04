"""Text-to-speech workflow declaration."""

from __future__ import annotations

from ..toolkit import HookType, Workflow, subscription
from . import hooks
from .config import CONFIG_DEFAULTS, CONFIG_SCHEMA, normalize_config

WORKFLOW = Workflow(
    id="tts",
    display_name="Text-to-Speech",
    produces_artifacts=True,
    config_schema=CONFIG_SCHEMA,
    config_defaults=CONFIG_DEFAULTS,
    config_normalizer=normalize_config,
    subscriptions=[
        subscription(HookType.PRE_PIPELINE, hooks.pre_pipeline),
        # Priority 0 runs after the text transforms, so speech is made from the final draft.
        subscription(HookType.POST_PIPELINE, hooks.post_pipeline),
        subscription(HookType.ON_DEMAND, hooks.on_demand),
        subscription(HookType.QUERY, hooks.query),
        subscription(HookType.UPLOAD, hooks.upload),
        subscription(HookType.REGENERATE, hooks.regenerate),
        subscription(HookType.REROLL_GEN, hooks.reroll_gen),
    ],
)
