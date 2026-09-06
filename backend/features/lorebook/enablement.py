"""Lorebook feature enablement decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def agentic_lorebook_active(
    settings: Mapping[str, Any],
    lorebook_entries: Sequence[Mapping[str, Any]],
    *,
    agent_on: bool,
) -> bool:
    """Return whether the Director should pick lorebook entries this turn."""
    if not bool(settings.get("agentic_lorebook_enabled", 0)):
        return False
    if not agent_on:
        return False
    return any(not entry.get("constant") for entry in lorebook_entries)
