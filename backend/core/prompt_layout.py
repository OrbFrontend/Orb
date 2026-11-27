"""The prompt layout's vocabulary: block shape, marker ids, section placeholders, and the blocks every layout starts from."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .macros import named_placeholders

LayoutRegion = Literal["prefix", "writer"]
LayoutRole = Literal["system", "user", "assistant"]

CUSTOM_KIND = "custom"
DEFAULT_LAYOUT_ID = "default"

WRITER_MARKERS: tuple[str, ...] = (
    "speaker",
    "selected_lore",
    "scene_guidance",
    "user_message",
    "depth_lore",
)
MARKER_LABELS: dict[str, str] = {
    "main_prompt": "Main prompt",
    "cast": "Group cast",
    "constant_lore": "Constant lore",
    "selected_lore": "Selected lore",
    "scene_guidance": "Scene guidance",
    "user_message": "User message",
    "depth_lore": "Depth lore",
    "speaker": "Group speaker",
}

# The engine markers whose text the user edits besides the Main prompt, each with the text a layout starts from. The text must
# name the marker's own placeholder, ``{{<kind>}}``, which stands for the engine's part; the block renders nothing while that is
# empty.
MARKER_TEMPLATES: dict[str, str] = {
    "constant_lore": "## Lorebook\n\n{{constant_lore}}",
    "selected_lore": "___\n\n**Lorebook**\n\n{{selected_lore}}",
    "depth_lore": "___\n\n**Lorebook (Depth)**\n\n{{depth_lore}}",
    "scene_guidance": "___\n\n**Scene Guidance**\n\n{{scene_guidance}}",
    "user_message": "___\n\n{{user_message}}",
}
# The Group cast marker's text is one member's card, rendered for each member whose card the group's context mode shows, where
# the mode places it. Its card placeholders stand for that member's fields; it names no placeholder of its own.
CAST_TEMPLATE = "## Character: {{char}}\n{{description}}\n\n{{personality}}\n\n## Example Dialogue\n{{examples}}"
# The group speaker's heading over the Writer request, rendered only on a group turn, scoped to the speaker: {{char}} is the
# speaker's name, {{instructions}} their card's post-history instructions, and {{card}} their card from the Group cast text where
# the context mode carries it after the history (Private), else empty. A paragraph after the first naming only empty
# placeholders is left out. The sheet line defers to the transcript without dating the sheet: reviewed mid-scene sheet updates
# can make a scene-start label inaccurate. The engine closes the same request with its own line.
SPEAKER_TEMPLATE = (
    "## You are writing as {{char}}\n\n"
    "Reference sheet for this scene. Where the transcript above shows it has changed "
    "— appearance, dress, injuries, what they carry — follow the transcript.\n{{card}}\n\n"
    "## Additional Instructions\n{{instructions}}"
)
EDITABLE_MARKERS: frozenset[str] = frozenset({"main_prompt", "cast", "speaker", *MARKER_TEMPLATES})

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
    *WRITER_MARKERS,
)


_MARKER_SEEDS: dict[str, str] = {"cast": CAST_TEMPLATE, "speaker": SPEAKER_TEMPLATE, **MARKER_TEMPLATES}


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


def default_role(region: LayoutRegion) -> LayoutRole:
    """The role a block takes by default: system text above the chat history, the Writer's user turn below it."""
    return "system" if region == "prefix" else "user"


def marker_placeholder(kind: str) -> str:
    """The placeholder an editable marker's text must name."""
    return "{{" + kind + "}}"


def cast_template(blocks: Iterable[LayoutBlock]) -> str:
    """The layout's member card template, switched on or off; the built-in one when the layout has no Group cast block or its
    text is blank, as a preset that blanks block text leaves it. An edit cannot blank it."""
    block = next((block for block in blocks if block.kind == "cast"), None)
    return marker_text(block) if block else CAST_TEMPLATE


def marker_text(block: LayoutBlock) -> str:
    """An editable marker's text; its built-in text while blank, as a preset that blanks block text leaves it."""
    return block.content if block.content.strip() else _MARKER_SEEDS.get(block.kind, "")


def required_placeholder(kind: str) -> str | None:
    """The placeholder a *kind* marker's text must keep, standing for the engine's part; None when it keeps none."""
    if kind in MARKER_TEMPLATES:
        return kind
    return "card" if kind == "speaker" else None


def marker_text_problem(kind: str, label: str, text: str) -> str:
    """Why *text* cannot become the text of a *kind* marker labelled *label*, or nothing."""
    if (name := required_placeholder(kind)) and not named_placeholders(text, (name,)):
        return f"The {label} block's text must keep {marker_placeholder(name)}, which stands for the engine's part."
    if kind == "cast" and not text.strip():
        return f"The {label} block needs text: it is each group member's card."
    if kind == "speaker" and not text.strip():
        return f"The {label} block needs text; switch it off to leave it out."
    return ""


def main_prompt_text(blocks: Iterable[LayoutBlock]) -> str | None:
    """The layout's Main prompt text: empty when the layout has none, None when the layout switches it off."""
    block = next((block for block in blocks if block.kind == "main_prompt"), None)
    if block is None:
        return ""
    return block.content if block.enabled else None


def default_layout(main_prompt: str = "") -> tuple[LayoutBlock, ...]:
    """The starting blocks: every marker and section block in the built-in order, the Main prompt holding *main_prompt*. A
    built-in block's id is its key, the same in every profile; a custom block's is a fresh uuid."""
    sections = {key: (label, text) for key, label, text in SECTION_BLOCKS}
    blocks: list[LayoutBlock] = []
    for index, key in enumerate(_SEED_ORDER):
        if key in sections:
            label, text = sections[key]
            blocks.append(LayoutBlock(id=key, kind=CUSTOM_KIND, label=label, content=text, sort_order=index))
        else:
            blocks.append(
                LayoutBlock(
                    id=key,
                    kind=key,
                    label=MARKER_LABELS[key],
                    content=main_prompt if key == "main_prompt" else _MARKER_SEEDS.get(key, ""),
                    role=default_role(marker_region(key)),
                    region=marker_region(key),
                    sort_order=index,
                )
            )
    return tuple(blocks)
