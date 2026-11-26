"""Render state fragments' current state for the passes that receive it."""

from __future__ import annotations

from collections.abc import Sequence

from ..core import StateFragment, StateView

STATE_BLOCK_HEADING = "**Current State**"


def render_state_block(fragments: Sequence[StateFragment], view: StateView, *, prior: StateView | None = None) -> str:
    """Render injected state under its heading, showing before-Writer changes as ``old -> new``."""
    body = render_state_body(fragments, view, prior=prior)
    return f"{STATE_BLOCK_HEADING}\n\n{body}" if body else ""


def render_state_body(fragments: Sequence[StateFragment], view: StateView, *, prior: StateView | None = None) -> str:
    """The state block's fragments without its heading; empty when no fragment holds anything."""
    parts: list[str] = []
    for fragment in fragments:
        entries = view.active(fragment.id)
        if not entries:
            continue
        if fragment.mode == "value" and len(entries) == 1:
            value = entries[0].text
            old = prior.active(fragment.id) if prior is not None else []
            if len(old) == 1 and old[0].text and old[0].text != value:
                value = f"{old[0].text} -> {value}"
            parts.append(f"{fragment.heading}: {value}")
        else:
            parts.append(fragment.heading + ":\n" + "\n".join(f"- {entry.text}" for entry in entries))
    return "\n\n".join(parts)
