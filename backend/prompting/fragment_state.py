"""Render state fragments' current state for the passes that receive it."""

from __future__ import annotations

from collections.abc import Sequence

from ..core import StateFragment, StateView

STATE_BLOCK_HEADING = "**Current State**"


def render_state_block(
    fragments: Sequence[StateFragment],
    view: StateView,
    *,
    prior: StateView | None = None,
) -> str:
    """One compact current-state block for a recipient, or ``""`` when nothing is set.

    *fragments* are the fragments that inject into this recipient, in order.
    A one-value fragment renders as ``Heading: value``; given *prior*, a value
    this turn changed renders as ``old -> new``, the transition the Writer saw
    for progressive fragments. A multiple-entry fragment -- or a one-value one
    still holding several entries after a mode switch -- renders as a list.
    """
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
    if not parts:
        return ""
    return "\n\n".join([STATE_BLOCK_HEADING, *parts])
