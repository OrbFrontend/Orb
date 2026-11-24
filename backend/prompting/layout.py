"""Render the user-ordered prompt layout: the prefix before history and the Writer's request after it."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ..core import ChatMessage, ContentPart, Macros
from ..core.prompt_layout import (
    CUSTOM_KIND,
    MARKER_TEMPLATES,
    SECTION_PLACEHOLDERS,
    LayoutBlock,
    LayoutRegion,
)


def _placeholder_re(names: Iterable[str]) -> re.Pattern[str]:
    return re.compile(r"\{\{(" + "|".join(names) + r")\}\}", re.IGNORECASE)


_SECTION_RE = _placeholder_re(SECTION_PLACEHOLDERS)
_EXAMPLE_SEPARATOR = "<START>"
_DIVIDER = "___\n\n"
# The Writer ships the shared tool schemas under tool_choice="none", which some routes rewrite to "auto" and some templates
# ignore; this line is what keeps the Writer writing prose there. "This turn" keeps it true when a later pass replays the request.
TOOL_NOTICE = "**Do not use tool or function calls this turn.**"


@dataclass(frozen=True, slots=True)
class SectionInputs:
    """The card and chat sections a block's placeholders stand for, macros resolved."""

    description: str = ""
    personality: str = ""
    # A solo card keeps a Character block's heading even with no description or personality.
    character_heading: bool = False
    scenario: str = ""
    examples: str = ""
    instructions: str = ""
    persona: str = ""


@dataclass(frozen=True, slots=True)
class PrefixInputs:
    """Each prefix marker's own text, plus the sections for placeholder blocks."""

    main_prompt: str = ""
    cast: str = ""
    constant_lore: str = ""
    sections: SectionInputs = field(default_factory=SectionInputs)


@dataclass(frozen=True, slots=True)
class WriterInputs:
    """Each Writer-request marker's own text, the engine lines that ride a marker, the sections for placeholder blocks, and the
    speaker frame around them."""

    selected_lore: str = ""
    decisions: str = ""
    scene_guidance: str = ""
    # Follows Scene guidance.
    current_state: str = ""
    # Lead the User message.
    tool_notice: str = ""
    length_nudge: str = ""
    cue: str = ""
    user_message: str = ""
    depth_lore: str = ""
    sections: SectionInputs = field(default_factory=SectionInputs)
    speaker_open: str = ""
    speaker_close: str = ""


def _render_marker(block: LayoutBlock, value: str, resolve: Callable[[str], str]) -> str:
    """A marker: its own text around *value* when the user edits it, else *value* as is."""
    if block.kind not in MARKER_TEMPLATES:
        return value
    if not value:
        return ""
    return _fill(block.content or MARKER_TEMPLATES[block.kind], {block.kind: value}, resolve)


def region_blocks(blocks: Iterable[LayoutBlock], region: LayoutRegion) -> list[LayoutBlock]:
    """*region*'s switched-on blocks in order."""
    return [block for block in sorted(blocks, key=lambda block: block.sort_order) if block.region == region and block.enabled]


def _resolver(macros: Macros | None) -> Callable[[str], str]:
    return macros.resolve_message if macros else (lambda text: text)


def section_names(text: str) -> frozenset[str]:
    """The section placeholders *text* names."""
    return frozenset(match.group(1).lower() for match in _SECTION_RE.finditer(text))


def _sentinel(name: str) -> str:
    return f"\x00orb-section-{name}\x00"


def _fill(template: str, values: Mapping[str, str], resolve: Callable[[str], str]) -> str:
    """Resolve *template*'s macros, then put each value in for its placeholder without a second macro pass.

    An empty section's placeholder leaves with one blank-line separator beside it, the one before it when there is one.
    """
    text = resolve(_placeholder_re(values).sub(lambda match: _sentinel(match.group(1).lower()), template)).strip()
    for name, value in values.items():
        sentinel = re.escape(_sentinel(name))
        if not value:
            text = re.sub(f"\n\n{sentinel}|{sentinel}\n\n|{sentinel}", "", text)
    for name, value in values.items():
        text = text.replace(_sentinel(name), value)
    return text


def _fill_examples(template: str, values: Mapping[str, str], resolve: Callable[[str], str]) -> str:
    """Fill *template* once per ``<START>`` example, keeping the blank lines between examples."""
    head, *chunks = values["examples"].split(_EXAMPLE_SEPARATOR)
    rendered = head
    for chunk in chunks:
        body = chunk.removeprefix("\n")
        example = body.rstrip("\n")
        if example:
            rendered += _fill(template, {**values, "examples": example}, resolve)
        rendered += body[len(example) :]
    return rendered


def _render_block(content: str, sections: SectionInputs, resolve: Callable[[str], str]) -> str:
    """A custom block's text. A block naming sections renders nothing when they are all empty, except a solo Character
    heading."""
    names = section_names(content)
    if not names:
        return resolve(content).strip()
    values = {name: getattr(sections, name) for name in names}
    if not any(values.values()):
        if names & {"description", "personality"} and sections.character_heading:
            return _fill(content, values, resolve).rstrip("\n")
        return ""
    if _EXAMPLE_SEPARATOR in values.get("examples", ""):
        return _fill_examples(content, values, resolve)
    return _fill(content, values, resolve)


def _join(head: str, tail: str) -> str:
    return f"{head}\n\n{tail}" if head else tail


def _merge_content(head: str, content: str | list[ContentPart]) -> str | list[ContentPart]:
    if isinstance(content, str):
        return _join(head, content)
    parts = list(content)
    if parts and parts[0]["type"] == "text":
        return [{"type": "text", "text": _join(head, parts[0]["text"])}, *parts[1:]]
    return [{"type": "text", "text": head}, *parts]


def render_prefix(blocks: Sequence[LayoutBlock], inputs: PrefixInputs, macros: Macros | None) -> list[ChatMessage]:
    """The system message plus the pre-history messages: every message before the first history row."""
    resolve = _resolver(macros)
    pieces: list[str] = []
    pre_history: list[ChatMessage] = []
    for block in region_blocks(blocks, "prefix"):
        if block.kind == CUSTOM_KIND:
            text = _render_block(block.content, inputs.sections, resolve)
        else:
            text = _render_marker(block, getattr(inputs, block.kind), resolve)
        if block.role == "system":
            if block.kind == "main_prompt" and not pieces:
                # The main prompt opens the system message, even when empty, so the next section keeps its separator.
                pieces.append(text)
            elif text:
                pieces.append(f"\n\n{text}" if pieces else text)
        elif text:
            if pre_history and pre_history[-1]["role"] == block.role:
                pre_history[-1] = {"role": block.role, "content": _join(str(pre_history[-1]["content"]), text)}
            else:
                pre_history.append({"role": block.role, "content": text})
    return [{"role": "system", "content": "".join(pieces)}, *pre_history]


def join_history(head: Sequence[ChatMessage], history: Sequence[ChatMessage]) -> list[ChatMessage]:
    """Append *history* to the rendered prefix, merging the last pre-history message into a first row of the same role."""
    messages = list(head)
    rows = list(history)
    if len(messages) > 1 and rows and rows[0]["role"] == messages[-1]["role"]:
        messages[-1] = {"role": rows[0]["role"], "content": _merge_content(str(messages[-1]["content"]), rows[0]["content"])}
        rows = rows[1:]
    return messages + rows


def render_writer_tail(blocks: Sequence[LayoutBlock], inputs: WriterInputs, macros: Macros | None) -> str:
    """The Writer's one user message: the speaker frame, the Writer-request blocks in order, then the closing line.

    Custom blocks roll inline macros fresh on every build; the Editor replays the built string, so both passes read the same
    rolls.
    """
    resolve = _resolver(macros._replace(seed="") if macros else None)
    tail = inputs.speaker_open
    for block in region_blocks(blocks, "writer"):
        if block.kind == "user_message":
            # The engine's own lines for this request lead the user's message wherever the layout places it.
            cue = f"## Your cue\n{inputs.cue}" if inputs.cue else ""
            tail += "".join(f"{line}\n\n" for line in (inputs.tool_notice, inputs.length_nudge, cue) if line)
        if block.kind == CUSTOM_KIND:
            text = _render_block(block.content, inputs.sections, resolve)
            if text:
                tail += f"{_DIVIDER}{text}\n\n"
            continue
        text = _render_marker(block, getattr(inputs, block.kind), resolve)
        if text:
            tail += f"{text}\n\n"
        if block.kind == "scene_guidance" and inputs.current_state:
            # The state block rides the Director stage's Scene guidance, as it does in the Inspector's injection block.
            tail += f"{_DIVIDER}{inputs.current_state}\n\n"
    return tail + inputs.speaker_close
