"""The prompt layout's vocabulary: block shape, marker ids, section placeholders, and the blocks every layout starts from."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

LayoutRegion = Literal["prefix", "writer"]
LayoutRole = Literal["system", "user", "assistant"]

CUSTOM_KIND = "custom"
DEFAULT_LAYOUT_ID = "default"

PREFIX_MARKERS: tuple[str, ...] = ("main_prompt", "cast", "constant_lore", "workflow_blocks")
WRITER_MARKERS: tuple[str, ...] = (
    "selected_lore",
    "decisions",
    "scene_guidance",
    "user_message",
    "depth_lore",
)
MARKERS: tuple[str, ...] = PREFIX_MARKERS + WRITER_MARKERS
MARKER_LABELS: dict[str, str] = {
    "main_prompt": "Main prompt",
    "cast": "Group cast",
    "constant_lore": "Constant lore",
    "workflow_blocks": "Workflow blocks",
    "selected_lore": "Selected lore",
    "decisions": "Decisions",
    "scene_guidance": "Scene guidance",
    "user_message": "User message",
    "depth_lore": "Depth lore",
}

# The engine markers whose text the user edits besides the Main prompt, each with the text a layout starts from. The text must
# name the marker's own placeholder, ``{{<kind>}}``, which stands for the engine's part; the block renders nothing while that is
# empty.
MARKER_TEMPLATES: dict[str, str] = {
    "constant_lore": "## Lorebook\n\n{{constant_lore}}",
    "selected_lore": "___\n\n**Lorebook**\n\n{{selected_lore}}",
    "depth_lore": "___\n\n**Lorebook (Depth)**\n\n{{depth_lore}}",
    "decisions": "___\n\n**Major Decisions**\n\n{{decisions}}",
    "scene_guidance": "___\n\n**Scene Guidance**\n\n{{scene_guidance}}",
    "user_message": "___\n\n{{user_message}}",
}
EDITABLE_MARKERS: frozenset[str] = frozenset({"main_prompt", *MARKER_TEMPLATES})

# Placeholders a block's text may name, each standing for one card or chat section.
SECTION_PLACEHOLDERS: tuple[str, ...] = ("description", "personality", "scenario", "examples", "instructions", "persona")

# The section blocks every new layout seeds: (id suffix, label, text).
SECTION_BLOCKS: tuple[tuple[str, str, str], ...] = (
    ("character", "Character", "## Character: {{char}}\n{{description}}\n\n{{personality}}"),
    ("scenario", "Scenario", "## Scenario\n{{scenario}}"),
    ("example_dialogue", "Example dialogue", "## Example Dialogue\n{{examples}}"),
    ("card_instructions", "Chat instructions", "## Additional Instructions\n{{instructions}}"),
    ("user_persona", "User persona", "## User: {{user}}\n{{persona}}"),
)

_SEED_ORDER: tuple[str, ...] = (
    "main_prompt",
    "character",
    "cast",
    "constant_lore",
    "scenario",
    "example_dialogue",
    "card_instructions",
    "user_persona",
    "workflow_blocks",
    *WRITER_MARKERS,
)


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


def marker_placeholder(kind: str) -> str:
    """The placeholder an editable marker's text must name."""
    return "{{" + kind + "}}"


def names_marker_placeholder(kind: str, text: str) -> bool:
    return marker_placeholder(kind).lower() in text.lower()


def main_prompt_text(blocks: Iterable[LayoutBlock]) -> str:
    """The layout's Main prompt text; empty when the layout has none."""
    return next((block.content for block in blocks if block.kind == "main_prompt"), "")


def default_layout(layout_id: str = DEFAULT_LAYOUT_ID, main_prompt: str = "") -> tuple[LayoutBlock, ...]:
    """The starting blocks: every marker and section block in the built-in order, the Main prompt holding *main_prompt*."""
    sections = {key: (label, text) for key, label, text in SECTION_BLOCKS}
    blocks: list[LayoutBlock] = []
    for index, key in enumerate(_SEED_ORDER):
        if key in sections:
            label, text = sections[key]
            blocks.append(
                LayoutBlock(id=marker_block_id(layout_id, key), kind=CUSTOM_KIND, label=label, content=text, sort_order=index)
            )
        else:
            blocks.append(
                LayoutBlock(
                    id=marker_block_id(layout_id, key),
                    kind=key,
                    label=MARKER_LABELS[key],
                    content=main_prompt if key == "main_prompt" else MARKER_TEMPLATES.get(key, ""),
                    region=marker_region(key),
                    sort_order=index,
                )
            )
    return tuple(blocks)
