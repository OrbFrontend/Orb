"""Render the user-ordered prompt layout: the prefix before history and the Writer's request after it."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from ..core import ChatMessage, ContentPart, Macros
from ..core.prompt_layout import (
    CUSTOM_KIND,
    MARKERS,
    MOVABLE_MARKERS,
    TOGGLEABLE_MARKERS,
    LayoutBlock,
    LayoutRegion,
    default_layout,
    marker_region,
)

_CONTENT_RE = re.compile(r"\{\{\s*content\s*\}\}", re.IGNORECASE)
_CONTENT_SENTINEL = "\x00orb-layout-content\x00"
_EXAMPLE_SEPARATOR = "<START>"
_DIVIDER = "___\n\n"
_TOOL_NOTICE = "**Do not use tool or function calls this turn.**\n\n"


@dataclass(frozen=True, slots=True)
class PrefixInputs:
    """Each prefix marker's own text, macros resolved; a template's ``{{content}}`` receives it."""

    main_prompt: str = ""
    character: str = ""
    cast: str = ""
    constant_lore: str = ""
    scenario: str = ""
    example_dialogue: str = ""
    card_instructions: str = ""
    user_persona: str = ""
    workflow_blocks: str = ""


@dataclass(frozen=True, slots=True)
class WriterInputs:
    """Each Writer-request marker's own text, plus the speaker frame around them."""

    selected_lore: str = ""
    scene_guidance: str = ""
    length_nudge: str = ""
    cue: str = ""
    user_message: str = ""
    depth_lore: str = ""
    card_instructions: str = ""
    tool_notice: bool = False
    speaker_open: str = ""
    speaker_close: str = ""


def _region_of(block: LayoutBlock) -> LayoutRegion:
    if block.kind == CUSTOM_KIND or block.kind in MOVABLE_MARKERS:
        return block.region
    return marker_region(block.kind)


def _is_on(block: LayoutBlock) -> bool:
    return block.enabled or (block.kind != CUSTOM_KIND and block.kind not in TOGGLEABLE_MARKERS)


def region_blocks(blocks: Iterable[LayoutBlock], region: LayoutRegion) -> list[LayoutBlock]:
    """*region*'s enabled blocks in order, with any marker the layout lacks appended at its region's end."""
    blocks = [block for block in blocks if block.kind == CUSTOM_KIND or block.kind in MARKERS]
    present = {block.kind for block in blocks}
    missing = [block for block in default_layout() if block.kind not in present]
    ordered = sorted(blocks, key=lambda block: block.sort_order) + missing
    return [block for block in ordered if _region_of(block) == region and _is_on(block)]


def _resolver(macros: Macros | None) -> Callable[[str], str]:
    return macros.resolve_message if macros else (lambda text: text)


def _fill(template: str, content: str, resolve: Callable[[str], str]) -> str:
    """Resolve *template*'s macros, then put *content* in for ``{{content}}`` without a second macro pass."""
    return resolve(_CONTENT_RE.sub(_CONTENT_SENTINEL, template)).replace(_CONTENT_SENTINEL, content)


def _fill_examples(template: str, examples: str, resolve: Callable[[str], str]) -> str:
    """Fill *template* once per ``<START>`` example, keeping the blank lines between examples."""
    if _EXAMPLE_SEPARATOR not in examples:
        return _fill(template, examples, resolve)
    head, *chunks = examples.split(_EXAMPLE_SEPARATOR)
    rendered = head
    for chunk in chunks:
        body = chunk.removeprefix("\n")
        example = body.rstrip("\n")
        if example:
            rendered += _fill(template, example, resolve)
        rendered += body[len(example) :]
    return rendered


def _render_marker(block: LayoutBlock, content: str, resolve: Callable[[str], str]) -> str:
    if not content:
        return ""
    if block.kind == "example_dialogue":
        return _fill_examples(block.content, content, resolve)
    return _fill(block.content, content, resolve)


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
            text = resolve(block.content).strip()
            if not text:
                continue
            if block.role == "system":
                pieces.append(f"\n\n{text}" if pieces else text)
            elif pre_history and pre_history[-1]["role"] == block.role:
                pre_history[-1] = {"role": block.role, "content": _join(str(pre_history[-1]["content"]), text)}
            else:
                pre_history.append({"role": block.role, "content": text})
            continue
        text = _render_marker(block, getattr(inputs, block.kind), resolve)
        if block.kind == "main_prompt" and not pieces:
            # The main prompt opens the system message, even when empty, so the next section keeps its separator.
            pieces.append(text)
        elif text:
            pieces.append(f"\n\n{text}" if pieces else text)
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

    Custom blocks and templates roll inline macros fresh on every build; the Editor replays the built string, so both passes
    read the same rolls.
    """
    resolve = _resolver(macros._replace(seed="") if macros else None)
    tail = inputs.speaker_open
    for block in region_blocks(blocks, "writer"):
        if block.kind == "length_nudge" and inputs.tool_notice:
            tail += _TOOL_NOTICE
        if block.kind == CUSTOM_KIND:
            text = resolve(block.content).strip()
            if text:
                tail += f"{_DIVIDER}{text}\n\n"
            continue
        text = _render_marker(block, getattr(inputs, block.kind), resolve)
        if text:
            tail += f"{text}\n\n"
    return tail + inputs.speaker_close
