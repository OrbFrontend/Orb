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
    return agent_on and bool(world and world.get("dynamic_enabled"))
