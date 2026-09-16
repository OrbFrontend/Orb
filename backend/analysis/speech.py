"""Pure speech selection under a message's roleplay convention."""

from __future__ import annotations

import re

from ..core.text_segmentation import (
    CLOSE_QUOTES,
    HARD_LINE_BREAK_RE,
    OPEN_QUOTES,
    PROTECTED_MARKUP_RE,
    TOGGLE_QUOTES,
    find_quote_spans,
    sentence_boundary_ends,
    split_paragraphs,
)
from .text.markup import classify_axes
from .text.roleplay import (
    THOUGHT_ATTRIBUTION,
    AxisStyle,
    Dialogue,
    Narration,
    emphasis_inner,
    is_inline_emphasis,
    span_role,
    strip_quotes,
)
from .text.roleplay_segmentation import (
    extract_block_spans,
    strip_ooc,
)

__all__ = ["speech_input", "speech_segments"]


def speech_input(text: str) -> str:
    """Prose eligible for speech, retaining markup needed to read convention."""
    text = strip_ooc(text)
    # Fences must go before quote scanning: code can contain unbalanced quotes.
    text = PROTECTED_MARKUP_RE.sub(lambda m: "\n\n" if m.group().startswith("```") else m.group(), text)
    quotes = find_quote_spans(text)
    for match in reversed(list(PROTECTED_MARKUP_RE.finditer(text))):
        # Formatting inside a spoken quote is part of that quote, not a reason
        # to cut the quote in half or discard the emphasized words.
        if any(start < match.start() and match.end() < end for start, end in quotes):
            continue
        text = text[: match.start()] + "\n\n" + text[match.end() :]
    # Remove only parentheses outside speech (including quotes inside an aside).
    quotes = find_quote_spans(text)
    depth = 0
    hidden_start = 0
    cuts: list[tuple[int, int]] = []
    quote_index = 0
    for i, char in enumerate(text):
        while quote_index < len(quotes) and quotes[quote_index][1] <= i:
            quote_index += 1
        in_quote = quote_index < len(quotes) and quotes[quote_index][0] <= i < quotes[quote_index][1]
        if in_quote:
            continue
        if char == "(":
            if depth == 0:
                hidden_start = i
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            if depth == 0:
                cuts.append((hidden_start, i + 1))
    if depth:
        cuts.append((hidden_start, len(text)))
    for start, end in reversed(cuts):
        text = text[:start] + " " + text[end:]
    return text


def _unmarked_speech(text: str, style: AxisStyle) -> bool:
    """Read plain chat when the model cannot identify either markup convention.

    Unknown is not a semantic narration label: the markup model also returns
    unknown/unknown for ordinary greetings. Keep marked or malformed RP out of
    this fallback; an apostrophe within a word is not a quotation delimiter.
    """
    if style.dialogue != Dialogue.UNKNOWN or style.narration != Narration.UNKNOWN:
        return False
    markers = OPEN_QUOTES | CLOSE_QUOTES | TOGGLE_QUOTES | frozenset("*_—`")
    for i, char in enumerate(text):
        if char == "’" and 0 < i < len(text) - 1 and text[i - 1].isalnum() and text[i + 1].isalnum():
            continue
        if char in markers:
            return False
    return any(char.isalnum() for char in text)


def _speech_spans(para: str) -> list[tuple[str, int, int]]:
    """Add boundary-delimited dash speech without reparsing quotes or beats.

    Inline emphasis can belong to dash speech. Quotes and block emphasis bound
    eligible runs, so dashes inside either are never mistaken for new dialogue.
    """
    spans = extract_block_spans(para)
    barriers = [
        (start, end)
        for i, (typ, start, end) in enumerate(spans)
        if typ == "SPEECH" or (typ == "EMPHASIS" and not is_inline_emphasis(spans, i, para))
    ]
    dashes: list[tuple[str, int, int]] = []
    cursor = 0
    for start, end in [*barriers, (len(para), len(para))]:
        # Normalizing hard breaks preserves offsets while keeping pairs on one
        # line, including Unicode line separators recognized by the span reader.
        run = HARD_LINE_BREAK_RE.sub(lambda m: "\n" * len(m.group()), para[cursor:start])
        boundary = 0
        for match in re.finditer(r"—([^—\n]+)—", run):
            prefix = run[boundary : match.start()]
            if not re.search(r"(?:^|[.!?\n])\s*$", prefix):
                continue
            dashes.append(("SPEECH", cursor + match.start(), cursor + match.end()))
            # A closing dialogue dash is itself a boundary for the next line.
            boundary = match.end()
        cursor = end
    if not dashes:
        return spans

    marked = [
        (typ, start, end)
        for typ, start, end in spans
        if typ != "NARRATION" and not any(start < b and a < end for _, a, b in dashes)
    ]
    result: list[tuple[str, int, int]] = []
    cursor = 0
    for typ, start, end in sorted(marked + dashes, key=lambda span: span[1]):
        if cursor < start:
            result.append(("NARRATION", cursor, start))
        result.append((typ, start, end))
        cursor = end
    if cursor < len(para):
        result.append(("NARRATION", cursor, len(para)))
    return result


def speech_segments(text: str, style: AxisStyle | None = None, *, input_prepared: bool = False) -> list[tuple[str, str]]:
    """Ordered speech/action text under the message's markup convention.

    Shares format consistency's quotation, emphasis and inline-role decisions.
    A convention labels the whole message, not individual sentences. Unmarked
    chat with both conventions unknown falls back to plain speech; a positive
    narration reading still excludes the unquoted prose.
    Parenthetical asides, OOC and protected formatting are never spoken.
    Set input_prepared only for the result of speech_input used to classify it.
    """
    if not input_prepared:
        text = speech_input(text)
    style = style or classify_axes(text)
    plain_speech = _unmarked_speech(text, style)
    # Both axes bare means narration and speech have no structural boundary.
    # Explicit quotes remain usable, but guessing the bare spans reads narration.
    dialogue = Dialogue.UNKNOWN if style.narration == Narration.BARE and style.dialogue == Dialogue.BARE else style.dialogue
    if plain_speech:
        dialogue = Dialogue.BARE
    segments: list[tuple[str, str]] = []

    for para in split_paragraphs(text):
        spans = _speech_spans(para)
        pending = ""
        silent_until = 0

        def flush() -> None:
            nonlocal pending
            spoken = " ".join(pending.split())
            if spoken:
                segments.append(("dialogue", spoken))
            pending = ""

        for i, (typ, start, end) in enumerate(spans):
            if end <= silent_until:
                continue
            raw = para[max(start, silent_until) : end]
            role = span_role(spans, i, dialogue, para)
            if (typ == "SPEECH" or (typ == "EMPHASIS" and role == "NARRATION")) and THOUGHT_ATTRIBUTION.match(para[end:]):
                flush()
                # The attribution is narration even in a bare-dialogue message.
                # Keep the following sentence eligible for speech.
                tail = para[end:]
                next_speech = next((s for kind, s, _ in spans[i + 1 :] if kind == "SPEECH"), len(para))
                silent_until = min(next_speech, end + next(sentence_boundary_ends(tail), len(tail)))
                continue
            if typ == "SPEECH":
                flush()
                pending = strip_quotes(raw)
                flush()
            elif role == "NARRATION" and typ == "EMPHASIS":
                flush()
                segments.append(("beat", emphasis_inner(raw)))
            elif role == "DIALOGUE" or (role == "EMPHASIS_INLINE" and pending):
                pending += raw
            else:
                flush()
        flush()
    return segments
