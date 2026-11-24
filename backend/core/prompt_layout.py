"""The prompt layout's vocabulary: block shape, marker ids, and the built-in blocks every layout starts from."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LayoutRegion = Literal["prefix", "writer"]
LayoutRole = Literal["system", "user", "assistant"]

CUSTOM_KIND = "custom"
DEFAULT_LAYOUT_ID = "default"

PREFIX_MARKERS: tuple[str, ...] = (
    "main_prompt",
    "character",
    "cast",
    "constant_lore",
    "scenario",
    "example_dialogue",
    "card_instructions",
    "user_persona",
    "workflow_blocks",
)
WRITER_MARKERS: tuple[str, ...] = ("selected_lore", "scene_guidance", "length_nudge", "cue", "user_message", "depth_lore")
MARKERS: tuple[str, ...] = PREFIX_MARKERS + WRITER_MARKERS
# A marker gets a toggle only when no other setting already switches it; the group context mode decides what ``cast`` carries.
TOGGLEABLE_MARKERS: frozenset[str] = frozenset(
    {"character", "scenario", "example_dialogue", "card_instructions", "user_persona"}
)
MOVABLE_MARKERS: frozenset[str] = frozenset({"card_instructions"})

MARKER_LABELS: dict[str, str] = {
    "main_prompt": "Main prompt",
    "character": "Character",
    "cast": "Group cast",
    "constant_lore": "Constant lore",
    "scenario": "Scenario",
    "example_dialogue": "Example dialogue",
    "card_instructions": "Chat instructions",
    "user_persona": "User persona",
    "workflow_blocks": "Workflow blocks",
    "selected_lore": "Selected lore",
    "scene_guidance": "Scene guidance",
    "length_nudge": "Length nudge",
    "cue": "Speaker cue",
    "user_message": "User message",
    "depth_lore": "Depth lore",
}

# Each marker's seeded template; ``{{content}}`` is the section's own text and a marker with no text renders nothing.
_DIVIDED = "___\n\n{{content}}"
MARKER_TEMPLATES: dict[str, str] = {
    "main_prompt": "{{content}}",
    "character": "## Character: {{char}}\n{{content}}",
    "cast": "{{content}}",
    "constant_lore": "{{content}}",
    "scenario": "## Scenario\n{{content}}",
    "example_dialogue": "## Example Dialogue\n{{content}}",
    "card_instructions": "## Additional Instructions\n{{content}}",
    "user_persona": "## User: {{user}}\n{{content}}",
    "workflow_blocks": "{{content}}",
    "selected_lore": _DIVIDED,
    "scene_guidance": _DIVIDED,
    "length_nudge": "{{content}}",
    "cue": "## Your cue\n{{content}}",
    "user_message": _DIVIDED,
    "depth_lore": _DIVIDED,
}


@dataclass(frozen=True, slots=True)
class LayoutBlock:
    id: str
    kind: str
    label: str = ""
    content: str = ""
    role: LayoutRole = "system"
    region: LayoutRegion = "prefix"
    sort_order: int = 0
    enabled: bool = True


def marker_region(kind: str) -> LayoutRegion:
    return "writer" if kind in WRITER_MARKERS else "prefix"


def marker_block_id(layout_id: str, kind: str) -> str:
    return f"{layout_id}:{kind}"


def default_layout(layout_id: str = DEFAULT_LAYOUT_ID) -> tuple[LayoutBlock, ...]:
    """The built-in blocks: every marker in order with its seeded template."""
    return tuple(
        LayoutBlock(
            id=marker_block_id(layout_id, kind),
            kind=kind,
            label=MARKER_LABELS[kind],
            content=MARKER_TEMPLATES[kind],
            region=marker_region(kind),
            sort_order=index,
        )
        for index, kind in enumerate(MARKERS)
    )
