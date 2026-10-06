"""Prose Rewriter secondary-workflow declaration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..toolkit import Workflow

WORKFLOW_ID = "prose_rewriter"

# The workflow toggle turns the rewriter on, which is what the rewrite button on a saved reply needs; this switch decides
# whether every new reply also runs through it.
CONFIG_DEFAULTS: dict[str, Any] = {"automatic": True}

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {"automatic": {"type": "boolean", "title": "Rewrite every reply automatically"}},
}


def normalize_config(raw: Mapping[str, Any] | None) -> dict:
    """Return the normalized UI and hook configuration."""
    raw = raw if isinstance(raw, Mapping) else {}
    automatic = raw.get("automatic")
    return {"automatic": automatic if isinstance(automatic, bool) else CONFIG_DEFAULTS["automatic"]}


# The post hook runs the local rewrite model, which sits below the toolkit, so
# the host adapter ``backend/workflows/prose_rewriter_host.py`` binds it.
WORKFLOW = Workflow(
    id=WORKFLOW_ID,
    display_name="Prose Rewriter",
    config_schema=CONFIG_SCHEMA,
    config_defaults=CONFIG_DEFAULTS,
    config_normalizer=normalize_config,
)
