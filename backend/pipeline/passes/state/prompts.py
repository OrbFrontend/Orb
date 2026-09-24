"""Request text for the ``update_state`` step."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ....core import MAX_ACTIVE_ENTRIES, MAX_STATE_TEXT_CHARS, StateFragment, StateView
from .._prompting import REASONING_GUIDANCE, tool_call_instruction

STATE_PREAMBLE = (
    "[OOC: Pause the roleplay and step out of character. The fields below are the story's saved state: "
    "standing records that return on every later reply. Your task now is to keep them accurate."
)
_AFTER_REPLY_TASK = "Update them from what the reply above actually showed."
_BEFORE_WRITER_TASK = "Update them for the reply about to be written, following the scene direction below."
_RULES = (
    "Leave a field empty to keep it exactly as it is -- most turns change only some fields, or none. "
    "Record only what the story has established, never a guess."
)


@dataclass(frozen=True, slots=True)
class AliasedEntry:
    """An active entry listed in the request under a short alias."""

    alias: str
    fragment_id: str
    entry_id: str


def entry_aliases(fragments: Sequence[StateFragment], view: StateView) -> list[AliasedEntry]:
    """Number the listed entries across the whole request: ``e1``, ``e2``, ...

    Only multiple-entry fragments list retirable entries. Entry ids never reach
    the model; the aliases are mapped back in code.
    """
    out: list[AliasedEntry] = []
    for fragment in fragments:
        if fragment.mode != "entries":
            continue
        for entry in view.active(fragment.id):
            out.append(AliasedEntry(f"e{len(out) + 1}", fragment.id, entry.entry_id))
    return out


def _field_section(fragment: StateFragment, view: StateView, aliases: Sequence[AliasedEntry]) -> str:
    entries = view.active(fragment.id)
    lines: list[str] = []
    if fragment.mode == "entries":
        lines.append(f"{fragment.heading} -- list, {len(entries)} of {MAX_ACTIVE_ENTRIES} entries (parameter: {fragment.id})")
        lines.append(f"What to record: {fragment.description}")
        by_entry = {alias.entry_id: alias.alias for alias in aliases if alias.fragment_id == fragment.id}
        lines.extend(f"- [{by_entry[entry.entry_id]}] {entry.text}" for entry in entries)
        if not entries:
            lines.append("(no entries yet)")
        lines.append(
            "Add only new entries. Put the id of an entry that no longer holds in `retire`; to correct one, "
            "retire it and add the corrected text."
        )
        if len(entries) >= MAX_ACTIVE_ENTRIES:
            lines.append("This list is full: retire entries before adding any.")
    else:
        lines.append(f"{fragment.heading} -- one value (parameter: {fragment.id})")
        lines.append(f"What to record: {fragment.description}")
        if not entries:
            lines.append("Current value: (none yet)")
        elif len(entries) == 1:
            lines.append(f"Current value: {entries[0].text}")
        else:
            lines.append("Current value (several entries; a new value replaces them all):")
            lines.extend(f"- {entry.text}" for entry in entries)
        lines.append("Write the complete new value only if it changed.")
    return "\n".join(lines)


def build_state_request(
    fragments: Sequence[StateFragment],
    view: StateView,
    aliases: Sequence[AliasedEntry],
    *,
    placement: str,
    tool_schema: dict,
    reasoning_on: bool = False,
    scene_direction: str = "",
    decision_guidance: str = "",
    user_message: str = "",
) -> str:
    """Build the ``update_state`` request for one call."""
    task = _BEFORE_WRITER_TASK if placement == "before_writer" else _AFTER_REPLY_TASK
    parts = [f"{STATE_PREAMBLE} {task} {_RULES}" + (REASONING_GUIDANCE if reasoning_on else "")]
    parts.extend(_field_section(fragment, view, aliases) for fragment in fragments)
    parts.append(f"Each value or entry may be at most {MAX_STATE_TEXT_CHARS} characters.")
    if decision_guidance:
        parts.append("___\n\n" + decision_guidance)
    if scene_direction:
        parts.append("___\n\n" + scene_direction)
    if user_message:
        parts.append(f'User\'s next message (context):\n"""{user_message}"""')
    # Each field section already names its parameter, so the order carries no labels.
    parts.append(tool_call_instruction("update_state", tool_schema))
    return "\n\n".join(parts) + "]"
