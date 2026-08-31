"""Dependency-free predicates for pipeline turn modes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..inference import LLMClient


def is_dual_model(agent_client: LLMClient | None) -> bool:
    """Return whether the Agent uses a separate endpoint."""
    return agent_client is not None


def agent_enabled(settings: Mapping[str, Any]) -> bool:
    """Return whether the global Agent toggle is on."""
    return bool(settings.get("enable_agent", 1))


def direction_note_recording_active(
    settings: Mapping[str, Any],
    direction_note_fragments: Sequence[Mapping[str, Any]],
    *,
    agent_on: bool,
) -> bool:
    """Return whether the direction-note step should record for this group."""
    return agent_on and bool(settings.get("direction_notes_record", 0)) and bool(direction_note_fragments)


def direction_note_injection_active(settings: Mapping[str, Any]) -> bool:
    """Return whether stored direction notes should be injected."""
    return (settings.get("direction_notes_inject", "off") or "off") != "off"


def direction_note_to_director(settings: Mapping[str, Any]) -> bool:
    """Return whether the Director should see stored direction notes."""
    return (settings.get("direction_notes_inject", "off") or "off") in ("director", "both")


def direction_note_to_writer(settings: Mapping[str, Any]) -> bool:
    """Return whether the Writer should see stored direction notes."""
    return (settings.get("direction_notes_inject", "off") or "off") in ("writer", "both")


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
