"""
predicates.py — Dependency-free turn predicates.

Three pure functions that answer "what mode is this turn in?":
``agent_enabled``, ``is_dual_model``, and ``resolve_persona_id``. They read
settings/conversation mappings and return a flag or id.

Sits below ``config`` (which imports the pass modules) so any module in the
package can call these without pulling in the heavier pass dependencies.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..core.personas import resolve_persona_id as _resolve_persona_id

if TYPE_CHECKING:
    from ..inference import LLMClient


def is_dual_model(agent_client: LLMClient | None) -> bool:
    """Return True when the agent runs on a separate endpoint (dual-model mode).

    Single-model: writer and agent share one endpoint and KV cache.
    Dual-model: director + editor run on their own endpoint with a separate KV cache.
    """
    return agent_client is not None


def agent_enabled(settings: Mapping[str, Any]) -> bool:
    """Return True when the global Agent toggle is on (default on).

    All agent-gated features (director, editor, length guard, feedback, mood
    persistence) call this function, so the default-on behavior stays consistent.
    """
    return bool(settings.get("enable_agent", 1))


def direction_note_recording_active(
    settings: Mapping[str, Any],
    direction_note_fragments: Sequence[Mapping[str, Any]],
    *,
    agent_on: bool,
) -> bool:
    """Return True when the direction-note step should record for the given fragment group.

    Gated by the global Agent toggle, the master Writing switch (``direction_notes_record``),
    and at least one enabled direction-note fragment in the group. Callers pass the fragments
    of a single timing (pre-writer or post-turn), so this answers per placement. The write
    side; injection of already-stored notes is independent (see
    :func:`direction_note_injection_active`).
    """
    return agent_on and bool(settings.get("direction_notes_record", 0)) and bool(direction_note_fragments)


def direction_note_injection_active(settings: Mapping[str, Any]) -> bool:
    """Return True when stored direction notes should be injected at all.

    The read side, decoupled from recording: notes keep injecting even while recording
    is off or their authoring fragment is disabled. Off only when the injection target is
    ``off``; who receives them is a further choice (see :func:`direction_note_to_director` /
    :func:`direction_note_to_writer`).
    """
    return (settings.get("direction_notes_inject", "off") or "off") != "off"


def direction_note_to_director(settings: Mapping[str, Any]) -> bool:
    """True when the director's ``direct_scene`` pass should see the stored notes, so it
    decides the scene consistent with the direction it established earlier."""
    return (settings.get("direction_notes_inject", "off") or "off") in ("director", "both")


def direction_note_to_writer(settings: Mapping[str, Any]) -> bool:
    """True when the stored notes should ride the writer's Scene Direction block."""
    return (settings.get("direction_notes_inject", "off") or "off") in ("writer", "both")


# Moved down to ``core/personas.py`` when the extension persona resource needed
# the same priority rule from below the ``pipeline/`` layer. Re-exported here so
# every existing import keeps working and there is still one definition.
resolve_persona_id = _resolve_persona_id
