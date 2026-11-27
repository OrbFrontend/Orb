"""Render the user-ordered prompt layout: the prefix before history and the Writer's request after it."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ..core import ChatMessage, ContentPart, Macros
from ..core.macros import named_placeholders, outside_literals
from ..core.prompt_layout import (
    CUSTOM_KIND,
    MARKER_TEMPLATES,
    SECTION_PLACEHOLDERS,
    LayoutBlock,
    LayoutRegion,
    LayoutRole,
    marker_text,
)


def _placeholder_re(names: Iterable[str]) -> re.Pattern[str]:
    return re.compile(r"\{\{(" + "|".join(names) + r")\}\}", re.IGNORECASE)


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
    group speaker's parts."""

    selected_lore: str = ""
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
    # A group turn renders the Group speaker block: the speaker's card where the Writer request carries it, and their card's
    # post-history instructions. The closing line ends the request.
    group_turn: bool = False
    speaker_card: str = ""
    speaker_instructions: str = ""
    speaker_closing: str = ""


def _render_marker(block: LayoutBlock, value: str, sections: SectionInputs, resolve: Callable[[str], str]) -> str:
    """A marker. The Main prompt is its raw text, with the sections it names filled; it keeps its whitespace, so text naming
    none keeps its bytes. An editable marker is its own text around *value*, with the sections it names filled. Any other
    marker is *value* as is."""
    if block.kind == "main_prompt":
        return _fill(value, _section_values(value, sections), resolve, strip=False)
    if block.kind not in MARKER_TEMPLATES:
        return value
    if not value:
        return ""
    text = block.content or MARKER_TEMPLATES[block.kind]
    return _fill(text, {**_section_values(text, sections), block.kind: value}, resolve)


def _render_speaker(block: LayoutBlock, inputs: WriterInputs, resolve: Callable[[str], str]) -> str:
    """The Group speaker block on a group turn, nothing otherwise: its text filled once, ``{{card}}`` and ``{{instructions}}`` standing
    for the speaker's, the other sections keeping their chat meaning. A paragraph after the first naming only empty
    placeholders is left out."""
    if not inputs.group_turn:
        return ""
    text = marker_text(block)
    values = {
        **_section_values(text, inputs.sections),
        "card": inputs.speaker_card,
        "instructions": inputs.speaker_instructions,
    }
    return _fill(text, values, resolve, by_paragraph=True)


def _section_values(text: str, sections: SectionInputs) -> dict[str, str]:
    """The sections *text* names, each with its value, for text that is filled once: the examples go in together, each
    ``<START>`` line dropped and a blank line between examples."""
    values = {name: getattr(sections, name) for name in section_names(text)}
    if "examples" in values:
        examples = (chunk.strip("\r\n") for chunk in values["examples"].split(_EXAMPLE_SEPARATOR))
        values["examples"] = "\n\n".join(chunk for chunk in examples if chunk)
    return values


def region_blocks(blocks: Iterable[LayoutBlock], region: LayoutRegion) -> list[LayoutBlock]:
    """*region*'s switched-on blocks in order."""
    return [block for block in sorted(blocks, key=lambda block: block.sort_order) if block.region == region and block.enabled]


def _resolver(macros: Macros | None, block: LayoutBlock) -> Callable[[str], str]:
    """*block*'s own resolver. A block rolls at its own place: a marker by its kind, which a layout has one of, a custom block
    by its id, so moving a block keeps its rolls."""
    if macros is None:
        return lambda text: text
    return macros.at(f"layout:{block.id if block.kind == CUSTOM_KIND else block.kind}").resolve_message


def section_names(text: str) -> frozenset[str]:
    """The section placeholders *text* names outside backticked literals and ``{{// ... }}`` notes."""
    return named_placeholders(text, SECTION_PLACEHOLDERS)


def _sentinel(name: str) -> str:
    return f"\x00orb-section-{name}\x00"


def _fill(
    template: str,
    values: Mapping[str, str],
    resolve: Callable[[str], str],
    *,
    strip: bool = True,
    by_paragraph: bool = False,
) -> str:
    """Resolve *template*'s macros, then put each value in for its placeholder without a second macro pass.

    A backticked placeholder stays literal, and one inside a note leaves with the note. An empty section's placeholder leaves
    with one blank-line separator beside it, the one before it when there is one. *by_paragraph* also leaves out each paragraph
    after the first whose placeholders are all empty, so a heading leaves with its empty section.
    """
    if not values:
        return resolve(template).strip() if strip else resolve(template)
    pattern = _placeholder_re(values)
    marked = outside_literals(template, lambda t: pattern.sub(lambda match: _sentinel(match.group(1).lower()), t))
    text = resolve(marked)
    if strip:
        text = text.strip()
    if by_paragraph:
        head, *rest = text.split("\n\n")
        text = "\n\n".join([head, *(paragraph for paragraph in rest if not _names_only_empty(paragraph, values))])
    for name, value in values.items():
        sentinel = re.escape(_sentinel(name))
        if not value:
            text = re.sub(f"\n\n{sentinel}|{sentinel}\n\n|{sentinel}", "", text)
    for name, value in values.items():
        text = text.replace(_sentinel(name), value)
    return text


def _names_only_empty(paragraph: str, values: Mapping[str, str]) -> bool:
    named = [value for name, value in values.items() if _sentinel(name) in paragraph]
    return bool(named) and not any(named)


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


def render_card_template(template: str, sections: SectionInputs, resolve: Callable[[str], str]) -> str:
    """A member card from the Group cast text: filled once, as a marker's text is, the examples together. A paragraph after the
    first naming only empty sections is left out; the card renders nothing while every section it names is empty."""
    values = _section_values(template, sections)
    if values and not any(values.values()):
        return ""
    return _fill(template, values, resolve, by_paragraph=True)


def _join(head: str, tail: str) -> str:
    return f"{head}\n\n{tail}" if head else tail


def _merge_content(head: str, content: str | list[ContentPart]) -> str | list[ContentPart]:
    if isinstance(content, str):
        return _join(head, content)
    parts = list(content)
    if parts and parts[0]["type"] == "text":
        return [{"type": "text", "text": _join(head, parts[0]["text"])}, *parts[1:]]
    return [{"type": "text", "text": head}, *parts]


def _block_text(block: LayoutBlock, inputs: PrefixInputs | WriterInputs, resolve: Callable[[str], str]) -> str:
    if block.kind == CUSTOM_KIND:
        return _render_block(block.content, inputs.sections, resolve)
    if block.kind == "speaker":
        return _render_speaker(block, inputs, resolve) if isinstance(inputs, WriterInputs) else ""
    return _render_marker(block, getattr(inputs, block.kind), inputs.sections, resolve)


def prefix_block_texts(
    blocks: Sequence[LayoutBlock], inputs: PrefixInputs, macros: Macros | None
) -> list[tuple[LayoutBlock, str]]:
    """Each switched-on prefix block in order, with the text it renders."""
    return [(block, _block_text(block, inputs, _resolver(macros, block))) for block in region_blocks(blocks, "prefix")]


def writer_block_texts(
    blocks: Sequence[LayoutBlock], inputs: WriterInputs, macros: Macros | None
) -> list[tuple[LayoutBlock, str]]:
    """Each switched-on Writer-request block in order, with the text it renders; Scene guidance carries the state block."""
    # Built fresh every turn: random and roll roll anew, a pick stays fixed for its block.
    fresh = macros._replace(fresh=True) if macros else None
    texts: list[tuple[LayoutBlock, str]] = []
    for block in region_blocks(blocks, "writer"):
        text = _block_text(block, inputs, _resolver(fresh, block))
        if block.kind == "scene_guidance" and inputs.current_state:
            # The state block rides the Director stage's Scene guidance, as it does in the Inspector's injection block.
            text = _join(text, f"{_DIVIDER}{inputs.current_state}")
        texts.append((block, text))
    return texts


def render_prefix(blocks: Sequence[LayoutBlock], inputs: PrefixInputs, macros: Macros | None) -> list[ChatMessage]:
    """Every message before the first history row: each block a message of its role, in order, adjacent blocks of one role
    sharing a message. No message is empty."""
    messages: list[ChatMessage] = []
    for block, text in prefix_block_texts(blocks, inputs, macros):
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
    """The Writer's messages after history: the Writer-request blocks in order.

    The engine's lines lead the User message, or precede the group speaker's closing line while the User message is switched
    off. The closing line is user text. The state block follows Scene guidance and leaves with it.

    Each block becomes a message of its role; adjacent blocks of one role share a message. Custom blocks roll inline macros
    fresh on every build; later passes replay the built messages, so they read the same rolls.
    """
    pieces: list[tuple[LayoutRole, str]] = []
    holder: int | None = None

    def add(role: LayoutRole, text: str, *, divided: bool = False) -> None:
        if divided and pieces and pieces[-1][0] == role:
            text = f"{_DIVIDER}{text}"
        pieces.append((role, f"{text}\n\n"))

    cue = f"## Your cue\n{inputs.cue}" if inputs.cue else ""
    engine_lines = [line for line in (inputs.tool_notice, inputs.length_nudge, cue) if line]
    writer_blocks = writer_block_texts(blocks, inputs, macros)
    for block, text in writer_blocks:
        if block.kind == "user_message":
            # The engine's own lines for this request lead the user's message wherever the layout places it.
            for line in engine_lines:
                add(block.role, line)
        if not text:
            continue
        # The divider separates a custom block from what precedes it in the same message.
        add(block.role, text, divided=block.kind == CUSTOM_KIND)
        if block.kind == "user_message":
            holder = len(pieces) - 1
    # With the User message switched off, the engine's lines still close the request as user text.
    user_message_off = not any(block.kind == "user_message" for block, _ in writer_blocks)
    closing: list[tuple[LayoutRole, str]] = [("user", f"{line}\n\n") for line in engine_lines if user_message_off]
    if inputs.speaker_closing:
        closing.append(("user", inputs.speaker_closing))
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
