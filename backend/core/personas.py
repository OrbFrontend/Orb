"""Canonical effective-persona precedence over already-loaded mappings.

This is not the persona feature. It owns only the host rule that selects an id:
conversation pin, then character-card pin, then the global active persona.
Persona loading, CRUD, projection, and consent remain in their owning layers.

The rule needs one identity in the pipeline and the community-extension resource
feature, which may not import sideways from one another. It therefore receives
plain mappings the callers already loaded and performs no database access.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def resolve_persona_id(
    conv: Mapping[str, Any],
    card: Mapping[str, Any] | None,
    settings: Mapping[str, Any],
) -> int | None:
    """Return the effective persona id for a turn.

    Priority: conversation pin -> character-card pin -> global active persona.
    """
    return conv.get("persona_lock_id") or (card.get("persona_lock_id") if card else None) or settings.get("active_persona_id")
