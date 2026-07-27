"""Which persona is in effect, as a pure function of already-loaded rows.

One rule, in one place, because two callers now need it and they sit on
opposite sides of the layer stack: the pipeline resolves the persona it injects
into a turn, and the community-extension ``context.persona.read`` resource
projects the persona a package may see. If those disagreed, a package would
read a description the model never got -- or, worse, the reverse.

Pure and mapping-shaped so it can live in the kernel: it reads three plain
dicts the caller already has and touches no database.
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
