"""Analysis-specific sentence, dialogue, and block segmentation."""

from __future__ import annotations

import re

from ...core.text_segmentation import (
    CLOSE_QUOTES,
    HARD_LINE_BREAK_RE,
    OPEN_QUOTES,
    PARA_SPLIT,
    SENT_SPLIT,
    TOGGLE_QUOTES,
    count_sentences,
    ends_with_question,
    ends_with_sentence_terminator,
    extract_unquoted_text,
    find_quote_spans,
    sentence_boundary_ends,
    split_paragraphs,
    split_sentence_units,
    split_sentences,
)

__all__ = [
    "HARD_LINE_BREAK_RE",
    "PARA_SPLIT",
    "SENT_SPLIT",
    "OPEN_QUOTES",
    "CLOSE_QUOTES",
    "TOGGLE_QUOTES",
    "EMPHASIS_RE",
    "split_paragraphs",
    "split_sentences",
    "split_sentence_units",
    "sentence_boundary_ends",
    "ends_with_sentence_terminator",
    "split_segment_sentences",
    "extract_narration",
    "split_narration_sentences",
    "strip_ooc",
    "find_quote_spans",
    "find_emphasis_spans",
    "extract_block_spans",
    "extract_blocks",
    "count_sentences",
    "ends_with_question",
]


def extract_narration(paragraph: str) -> str:
    """Return paragraph text outside balanced quoted spans."""
    return extract_unquoted_text(paragraph)


def split_narration_sentences(text: str) -> list[str]:
    """Return source-contiguous narration sentences with dialogue removed."""
    sentences: list[str] = []

    def append_run(run: str, *, touches_dialogue: bool) -> None:
        units = split_sentence_units(run)
        if touches_dialogue:
            # In punctuation-outside-quote styles (``"Enough". Then``), a
            # punctuation-only narration block belongs to the quote.
            units = [unit for unit in units if any(ch.isalnum() for ch in unit)]
        sentences.extend(units)

    for paragraph in split_paragraphs(text):
        run_start: int | None = None
        run_end = 0
        after_dialogue = False
        for typ, start, end in extract_block_spans(paragraph):
            if typ == "SPEECH":
                if run_start is not None:
                    append_run(paragraph[run_start:run_end], touches_dialogue=True)
                    run_start = None
                after_dialogue = True
                continue
            if run_start is None:
                run_start = start
            run_end = end
        if run_start is not None:
            append_run(paragraph[run_start:run_end], touches_dialogue=after_dialogue)
    return sentences


_OOC_START_RE = re.compile(r"\[\s*ooc\b", re.IGNORECASE)


def strip_ooc(text: str) -> str:
    """Remove balanced ``[OOC: ...]`` asides; an unclosed tag removes its tail."""
    pieces: list[str] = []
    cursor = 0
    while match := _OOC_START_RE.search(text, cursor):
        pieces.append(text[cursor : match.start()])
        depth = 0
        end = len(text)
        for i in range(match.start(), len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        pieces.append(" ")
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


_NO_LINE_BREAK_ASTERISK = r"[^*\n\v\f\r\x1c-\x1e\x85\u2028\u2029]"
_NO_LINE_BREAK_UNDERSCORE = r"[^_\n\v\f\r\x1c-\x1e\x85\u2028\u2029]"
EMPHASIS_RE = re.compile(
    rf"(?<![\w*\\])\*(?![\s*])({_NO_LINE_BREAK_ASTERISK}+?)\*(?![\w*])"
    rf"|(?<![\w_\\])_(?![\s_])({_NO_LINE_BREAK_UNDERSCORE}+?)_(?![\w_])"
)


def _line_start_before(text: str, offset: int) -> int:
    start = 0
    for match in HARD_LINE_BREAK_RE.finditer(text, 0, offset):
        start = match.end()
    return start


def find_emphasis_spans(text: str) -> list[tuple[int, int]]:
    """Return single-marker emphasis spans, excluding bullets, escapes, and bold."""
    spans: list[tuple[int, int]] = []
    for match in EMPHASIS_RE.finditer(text):
        if match.group(0).startswith("*"):
            prefix = text[_line_start_before(text, match.start()) : match.start()]
            after_star = match.start() + 1
            if prefix.strip() == "" and after_star < len(text) and text[after_star] in " \t":
                continue
        spans.append((match.start(), match.end()))
    return spans


def extract_block_spans(paragraph: str) -> list[tuple[str, int, int]]:
    """Tile a paragraph into raw ``SPEECH``/``EMPHASIS``/``NARRATION`` spans."""
    quote_spans = find_quote_spans(paragraph)
    emphasis_spans = [
        (start, end)
        for start, end in find_emphasis_spans(paragraph)
        if not any(start < quote_end and quote_start < end for quote_start, quote_end in quote_spans)
    ]
    typed = sorted(
        [(start, end, "SPEECH") for start, end in quote_spans] + [(start, end, "EMPHASIS") for start, end in emphasis_spans]
    )

    spans: list[tuple[str, int, int]] = []
    cursor = 0
    for start, end, typ in typed:
        if start < cursor:
            continue
        if cursor < start:
            spans.append(("NARRATION", cursor, start))
        spans.append((typ, start, end))
        cursor = end
    if cursor < len(paragraph):
        spans.append(("NARRATION", cursor, len(paragraph)))
    return spans


def split_segment_sentences(text: str) -> list[str]:
    """Split typed prose blocks into source-contiguous, linebreak-free sentences."""
    segments: list[str] = []
    for paragraph in split_paragraphs(text):
        for _typ, start, end in extract_block_spans(paragraph):
            segments.extend(split_sentence_units(paragraph[start:end]))
    return segments


def extract_blocks(paragraph: str) -> list[tuple[str, str]]:
    """Return non-empty typed block strings."""
    blocks: list[tuple[str, str]] = []
    for typ, start, end in extract_block_spans(paragraph):
        block = paragraph[start:end].strip()
        if block:
            blocks.append((typ, block))
    return blocks
