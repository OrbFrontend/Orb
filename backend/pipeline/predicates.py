"""Dependency-free predicates for pipeline turn modes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..inference import LLMClient


def is_dual_model(agent_client: LLMClient | None) -> bool:
    """Return whether the Agent uses a separate endpoint."""
    return agent_client is not None


def agent_enabled(settings: Mapping[str, Any]) -> bool:
    """Return whether the global Agent toggle is on."""
    return bool(settings.get("enable_agent", 1))


def world_proposal_active(world: Mapping[str, Any] | None, *, agent_on: bool) -> bool:
    """Return whether this turn may propose changes to *world*."""
    return agent_on and bool(world and world.get("enabled") and world.get("dynamic_enabled"))


def resolve_persona_id(
    conv: Mapping[str, Any],
    card: Mapping[str, Any] | None,
    settings: Mapping[str, Any],
) -> int | None:
    """Return the effective persona id for a turn.

    Priority: conversation pin → character-card pin → global active persona.
    """
    return conv.get("persona_lock_id") or (card.get("persona_lock_id") if card else None) or settings.get("active_persona_id")
