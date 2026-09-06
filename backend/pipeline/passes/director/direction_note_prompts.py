"""Instruction and rendering contracts for direction notes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .._prompting import REASONING_GUIDANCE, tool_call_instruction

DIRECTION_NOTE_PREAMBLE = (
    "[OOC: Pause the roleplay and step out of character. The categories below are standing records "
    "of lasting direction for this roleplay, and your task now is to update them. Work through each "
    "one and record into it anything from what just happened that must hold for the rest of the "
    "roleplay. Whatever you record is permanent: it returns on every later reply and steers the "
    "rest of the story, so a category takes only what genuinely must constrain what follows -- if "
    "nothing this turn belongs in a category, leave it empty. Record only the bare fact in each "
    "category -- no leading label, category name, or turn number. Those are attached automatically; "
    "where earlier entries appear tagged that way, the tag is for your reference only."
)


def _direction_notes_lines(notes: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for note in notes:
        turn = note.get("turn_index")
        tag = f"{note['interactive_fragment_label']}, turn {turn}" if turn is not None else note["interactive_fragment_label"]
        lines.append(f"- ({tag}) {note['content']}")
    return "\n".join(lines)


def render_direction_notes_block(notes: Sequence[Mapping[str, Any]]) -> str:
    """Render active direction notes as a Scene Direction sub-block."""
    if not notes:
        return ""
    return f"**Direction Notes**\n{_direction_notes_lines(notes)}"


def build_direction_note_prompt(
    active_notes: Sequence[Mapping[str, Any]],
    direction_note_fragments: Sequence[Mapping[str, Any]],
    *,
    inj_block: str | None = None,
    reasoning_on: bool = False,
    tool_schema: dict | None = None,
) -> str:
    """Build the direction-note recording request."""
    preamble = DIRECTION_NOTE_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]
    if active_notes:
        parts.append("Already recorded (do not repeat these):\n" + _direction_notes_lines(active_notes))
    if inj_block:
        parts.append(inj_block)
    if tool_schema is not None:
        labels = {
            fragment["id"]: (fragment.get("injection_label") or fragment.get("label") or "").strip()
            for fragment in direction_note_fragments
        }
        parts.append(tool_call_instruction("record_direction_note", tool_schema, labels=labels))
    return "\n\n".join(parts) + "]"
