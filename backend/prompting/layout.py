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
    LayoutRole,
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
    """Every message before the first history row: each block a message of its role, in order, adjacent blocks of one role
    sharing a message. No message is empty."""
    resolve = _resolver(macros)
    messages: list[ChatMessage] = []
    for block in region_blocks(blocks, "prefix"):
        if block.kind == CUSTOM_KIND:
            text = _render_block(block.content, inputs.sections, resolve)
        else:
            text = _render_marker(block, getattr(inputs, block.kind), resolve)
        if block.kind == "main_prompt" and block.role == "system" and not messages:
            # The main prompt opens the prompt, even when empty, so the next system block keeps its separator.
            messages.append({"role": "system", "content": text})
        elif text and messages and messages[-1]["role"] == block.role:
            messages[-1] = {"role": block.role, "content": f"{messages[-1]['content']}\n\n{text}"}
        elif text:
            messages.append({"role": block.role, "content": text})
    return [message for message in messages if message["content"]]


def join_history(head: Sequence[ChatMessage], history: Sequence[ChatMessage]) -> list[ChatMessage]:
    """Append *history* to the rendered prefix, merging the last prefix message into a first row of the same role."""
    messages = list(head)
    rows = list(history)
    if messages and rows and rows[0]["role"] == messages[-1]["role"]:
        messages[-1] = {"role": rows[0]["role"], "content": _merge_content(str(messages[-1]["content"]), rows[0]["content"])}
        rows = rows[1:]
    return messages + rows


@dataclass(frozen=True, slots=True)
class WriterRequest:
    """The Writer's request: the messages after history, and which one the turn's attachments ride."""

    messages: tuple[ChatMessage, ...]
    # The message holding the User message when that is a user message, else the last user message; None when there is none.
    attach_to: int | None


def render_writer_request(blocks: Sequence[LayoutBlock], inputs: WriterInputs, macros: Macros | None) -> WriterRequest:
    """The Writer's messages after history: the speaker frame, the Writer-request blocks in order, then the closing line.

    The engine's lines lead the User message, or precede the closing line while the User message is switched off. The state
    block follows Scene guidance and leaves with it.

    Each block becomes a message of its role; adjacent blocks of one role share a message. The speaker frame and the closing
    line are user text. Custom blocks roll inline macros fresh on every build; later passes replay the built messages, so they
    read the same rolls.
    """
    resolve = _resolver(macros._replace(seed="") if macros else None)
    pieces: list[tuple[LayoutRole, str]] = []
    holder: int | None = None

    def add(role: LayoutRole, text: str, *, divided: bool = False) -> None:
        if divided and pieces and pieces[-1][0] == role:
            text = f"{_DIVIDER}{text}"
        pieces.append((role, f"{text}\n\n"))

    cue = f"## Your cue\n{inputs.cue}" if inputs.cue else ""
    engine_lines = [line for line in (inputs.tool_notice, inputs.length_nudge, cue) if line]
    writer_blocks = region_blocks(blocks, "writer")
    if inputs.speaker_open:
        pieces.append(("user", inputs.speaker_open))
    for block in writer_blocks:
        if block.kind == "user_message":
            # The engine's own lines for this request lead the user's message wherever the layout places it.
            for line in engine_lines:
                add(block.role, line)
        if block.kind == CUSTOM_KIND:
            text = _render_block(block.content, inputs.sections, resolve)
            if text:
                # The divider separates a custom block from what precedes it in the same message.
                add(block.role, text, divided=True)
            continue
        text = _render_marker(block, getattr(inputs, block.kind), resolve)
        if text:
            add(block.role, text)
            if block.kind == "user_message":
                holder = len(pieces) - 1
        if block.kind == "scene_guidance" and inputs.current_state:
            # The state block rides the Director stage's Scene guidance, as it does in the Inspector's injection block.
            add(block.role, f"{_DIVIDER}{inputs.current_state}")
    # With the User message switched off, the engine's lines still close the request as user text.
    user_message_off = not any(block.kind == "user_message" for block in writer_blocks)
    closing: list[tuple[LayoutRole, str]] = [("user", f"{line}\n\n") for line in engine_lines if user_message_off]
    if inputs.speaker_close:
        closing.append(("user", inputs.speaker_close))
    # The closing lines go before a trailing run of assistant blocks, which the reply continues.
    cut = prefill_start([role for role, _ in pieces])
    pieces[cut:cut] = closing
    if holder is not None and holder >= cut:
        holder += len(closing)

    messages: list[ChatMessage] = []
    attach_to: int | None = None
    for index, (role, text) in enumerate(pieces):
        if messages and messages[-1]["role"] == role:
            messages[-1] = {"role": role, "content": str(messages[-1]["content"]) + text}
        else:
            if messages:
                # A message that ends before another opens drops its trailing separator.
                messages[-1] = {"role": messages[-1]["role"], "content": str(messages[-1]["content"]).rstrip()}
            messages.append({"role": role, "content": text})
        if index == holder and role == "user":
            attach_to = len(messages) - 1
    if not messages:
        # Nothing rendered: the request is still the one empty user message the turn's attachments ride.
        return WriterRequest(({"role": "user", "content": ""},), 0)
    if messages[-1]["role"] == "assistant":
        # The reply continues this text, so it keeps no trailing separator (Anthropic also refuses one).
        messages[-1] = {"role": "assistant", "content": str(messages[-1]["content"]).rstrip()}
    if attach_to is None:
        attach_to = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "user"), None)
    return WriterRequest(tuple(messages), attach_to)


def prefill_start(roles: Sequence[str]) -> int:
    """Where the trailing run of assistant roles, which the reply continues, starts; ``len(roles)`` when there is none."""
    cut = len(roles)
    while cut and roles[cut - 1] == "assistant":
        cut -= 1
    return cut


def replay_reply(request: Sequence[ChatMessage], reply: str) -> list[ChatMessage]:
    """*request* followed by the reply it got, as a later pass replays them: a request ending on assistant text is the reply's
    own opening, so the reply continues that message."""
    messages = list(request)
    if messages and messages[-1]["role"] == "assistant" and isinstance(messages[-1]["content"], str):
        return [*messages[:-1], {"role": "assistant", "content": messages[-1]["content"] + reply}]
    return [*messages, {"role": "assistant", "content": reply}]
