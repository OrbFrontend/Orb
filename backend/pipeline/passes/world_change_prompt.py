"""Instruction prompt owned by the Dynamic Worlds pass."""

from __future__ import annotations

from ._prompting import REASONING_GUIDANCE, tool_call_instruction

WORLD_CHANGE_PREAMBLE = (
    "[OOC: Pause the roleplay and step out of character. Review the exchange above and decide whether it "
    "added anything to a World's long-term memory. Use the World catalog below. Leave operations empty "
    "when nothing durable was established."
)

WORLD_CHANGE_RULES = (
    "Record only durable facts established by the exchange for long-term memory. Most turns add nothing; "
    "leave operations empty when nothing qualifies.\n"
    "- Do not record plans, guesses, possibilities, or facts introduced only in the assistant's reply "
    "until the user takes them up.\n"
    "- Preserve uncertainty and attribution: record rumors, beliefs, and disputed claims as such.\n"
    "- Write concise factual notes, not narrative prose.\n"
    "- Create only new information. Revise or retract only when an existing entry is no longer accurate; "
    "never duplicate, reword, or add detail to a correct entry."
)

WORLD_CHANGE_CATALOG_HEADER = (
    "**Current World memory** -- each `##` heading is a World and its current entries. Headings use "
    "`## <name> [world_id: <id>]`; for a `create` with more than one World, copy that id into "
    "`target_world`. Each entry's stable numeric id appears in brackets, `[id]`; use it as "
    "`target_entry_id` for `revise` or `retract`. `Authored` is user-written memory; `Dynamic World "
    "State` is accepted Agent-managed memory."
)


def build_world_change_prompt(
    catalog: str,
    *,
    original_user_message: str = "",
    reasoning_on: bool = False,
    tool_schema: dict | None = None,
) -> str:
    """Build the post-turn Dynamic Worlds proposal request."""
    preamble = WORLD_CHANGE_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble, WORLD_CHANGE_RULES]
    if original_user_message:
        parts.append(
            "The user turn above is Orb's own instruction to the writer, not something the user said. "
            f'Judge this as the user\'s message instead:\n"""{original_user_message}"""'
        )
    if catalog:
        parts.append(f"{WORLD_CHANGE_CATALOG_HEADER}\n{catalog}")
    if tool_schema is not None:
        parts.append(tool_call_instruction("propose_world_changes", tool_schema))
    return "\n\n".join(parts) + "]"
