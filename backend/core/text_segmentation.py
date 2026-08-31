"""Dependency-free sentence and quotation primitives."""

from __future__ import annotations

import re
from collections.abc import Iterator

__all__ = [
    "CLOSE_QUOTES",
    "HARD_LINE_BREAK_RE",
    "OPEN_QUOTES",
    "PARA_SPLIT",
    "SENT_SPLIT",
    "TOGGLE_QUOTES",
    "count_sentences",
    "ends_with_question",
    "ends_with_sentence_terminator",
    "extract_unquoted_text",
    "find_quote_spans",
    "remove_quoted_spans",
    "sentence_boundary_ends",
    "split_paragraphs",
    "split_sentence_units",
    "split_sentences",
]

# Python ``str.splitlines`` recognizes this complete set.  Keep an explicit
# pattern for callers that need to preserve or inspect the separators.
HARD_LINE_BREAK_RE = re.compile(r"\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]")
PARA_SPLIT = re.compile(r"(?:\r\n|[\n\r\x85\u2028\u2029])\s*(?:\r\n|[\n\r\x85\u2028\u2029])")

# Compatibility pattern for older consumers that call ``SENT_SPLIT.split``.
# Public splitting uses the scanner below for abbreviations and Unicode marks.
SENT_SPLIT = re.compile(
    r"(?:(?<=[.!?…])[\"\u201d\u2019'*_)\]]*\s+|(?:\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029])+)",
)

_QUOTE_PAIRS = {
    "“": "”",
    "‘": "’",
    "«": "»",
    "‹": "›",
    "「": "」",
    "『": "』",
    "„": "“",
    "‚": "‘",
}
OPEN_QUOTES = frozenset(_QUOTE_PAIRS)
CLOSE_QUOTES = frozenset(_QUOTE_PAIRS.values())
TOGGLE_QUOTES = frozenset({'"'})

_TERMINATORS = frozenset(".!?…。！？؟۔｡．।॥")
_QUESTION_TERMINATORS = frozenset("?？؟")
_TIGHT_TERMINATORS = frozenset("…。！？؟۔｡．।॥")
_TRAILING_MARKERS = frozenset("'»›」』*_)]}>") | CLOSE_QUOTES | TOGGLE_QUOTES
_TRAILING_TERMINALS = "!.…。！۔｡．।॥"

_ALWAYS_NONTERMINAL_ABBREVIATIONS = frozenset({"e.g.", "i.e.", "a.k.a.", "vs.", "v.", "cf."})
_LOWERCASE_CONTINUATION_ABBREVIATIONS = frozenset(
    {"a.m.", "p.m.", "approx.", "dept.", "est.", "misc.", "incl.", "esp.", "min.", "max.", "ref.", "sec."}
)
_TITLE_ABBREVIATIONS = frozenset(
    {
        "mr.",
        "mrs.",
        "ms.",
        "mx.",
        "dr.",
        "prof.",
        "rev.",
        "hon.",
        "pres.",
        "gov.",
        "sen.",
        "rep.",
        "sr.",
        "jr.",
        "st.",
        "mt.",
        "capt.",
        "cpt.",
        "lt.",
        "col.",
        "gen.",
        "sgt.",
        "adm.",
        "maj.",
    }
)
_NUMBER_ABBREVIATIONS = frozenset(
    {
        "no.",
        "fig.",
        "eq.",
        "ch.",
        "vol.",
        "pp.",
        "jan.",
        "feb.",
        "mar.",
        "apr.",
        "jun.",
        "jul.",
        "aug.",
        "sep.",
        "sept.",
        "oct.",
        "nov.",
        "dec.",
    }
)
_ABBREVIATION_BEFORE_PERIOD = re.compile(r"(?:[^\W\d_]+\.)+$", re.UNICODE)


def _period_is_nonterminal(text: str, period: int, next_char: int) -> bool:
    if period > 0 and period + 1 < len(text) and text[period - 1].isdigit() and text[period + 1].isdigit():
        return True

    match = _ABBREVIATION_BEFORE_PERIOD.search(text[: period + 1])
    if match is None:
        return False
    raw_abbreviation = match.group(0)
    abbreviation = raw_abbreviation.casefold()
    next_value = text[next_char] if next_char < len(text) else ""

    if abbreviation in _ALWAYS_NONTERMINAL_ABBREVIATIONS:
        return True
    if abbreviation in _TITLE_ABBREVIATIONS and next_value.isalnum():
        return True
    if abbreviation in _NUMBER_ABBREVIATIONS and next_value.isdigit():
        return True

    letters = raw_abbreviation.replace(".", "")
    if next_value.isalnum() and (len(letters) == 1 or (abbreviation.count(".") >= 2 and letters.isupper())):
        return True
    return abbreviation in _LOWERCASE_CONTINUATION_ABBREVIATIONS and next_value.islower()


def sentence_boundary_ends(text: str) -> Iterator[int]:
    """Yield lossless exclusive ends of punctuation-complete sentences.

    This iterator does not manufacture a sentence object and may include
    separator whitespace in its offsets.  Use :func:`split_sentence_units`
    when consuming sentence text; that function removes all line separators.
    """
    i = 0
    size = len(text)
    while i < size:
        if text[i] not in _TERMINATORS:
            i += 1
            continue

        terminal_end = i + 1
        while terminal_end < size and text[terminal_end] in _TERMINATORS:
            terminal_end += 1

        marker_end = terminal_end
        while marker_end < size and text[marker_end] in _TRAILING_MARKERS:
            marker_end += 1

        boundary_end = marker_end
        while boundary_end < size and text[boundary_end].isspace():
            boundary_end += 1
        has_separator = boundary_end > marker_end
        allows_tight_boundary = any(ch in _TIGHT_TERMINATORS for ch in text[i:terminal_end]) or (
            marker_end < size and text[marker_end].isupper() and any(ch in ".!?" for ch in text[i:terminal_end])
        )
        if not has_separator and not (allows_tight_boundary and marker_end < size):
            i = terminal_end
            continue

        only_one_period = terminal_end == i + 1 and text[i] == "."
        if not (only_one_period and _period_is_nonterminal(text, i, boundary_end)):
            yield boundary_end
            i = boundary_end
        else:
            i = terminal_end


def _split_sentence_line(text: str) -> list[str]:
    units: list[str] = []
    start = 0
    for end in sentence_boundary_ends(text):
        unit = text[start:end].strip()
        if unit:
            units.append(unit)
        start = end
    tail = text[start:].strip()
    if tail:
        units.append(tail)
    return units


def split_sentence_units(text: str) -> list[str]:
    """Split text into sentence units with line breaks as hard separators."""
    units: list[str] = []
    for line in text.splitlines():
        units.extend(_split_sentence_line(line))
    return units


def ends_with_sentence_terminator(text: str) -> bool:
    trimmed = text.rstrip()
    while trimmed and trimmed[-1] in _TRAILING_MARKERS:
        trimmed = trimmed[:-1].rstrip()
    return bool(trimmed and trimmed[-1] in _TERMINATORS)


def split_paragraphs(text: str) -> list[str]:
    """Split on blank-line paragraph breaks, dropping empty paragraphs."""
    return [paragraph for paragraph in PARA_SPLIT.split(text.strip()) if paragraph.strip()]


def split_sentences(text: str) -> list[str]:
    """Split text into sentences; no returned value contains a line break."""
    sentences: list[str] = []
    for paragraph in split_paragraphs(text):
        sentences.extend(split_sentence_units(paragraph))
    return sentences


def find_quote_spans(text: str) -> list[tuple[int, int]]:
    """Return balanced maximal dialogue spans, including their outer marks."""
    spans: list[tuple[int, int]] = []
    stack: list[str] = []
    outer_start = 0
    for i, ch in enumerate(text):
        if ch in TOGGLE_QUOTES:
            slashes = 0
            j = i - 1
            while j >= 0 and text[j] == "\\":
                slashes += 1
                j -= 1
            if slashes % 2:
                continue

        if ch == "’" and i > 0 and i + 1 < len(text) and text[i - 1].isalnum() and text[i + 1].isalnum():
            continue
        if ch in TOGGLE_QUOTES and not stack and i > 0 and text[i - 1].isdigit():
            continue

        if stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                spans.append((outer_start, i + 1))
            continue

        if ch in TOGGLE_QUOTES:
            if not stack:
                outer_start = i
            stack.append(ch)
        elif ch in OPEN_QUOTES:
            if not stack:
                outer_start = i
            stack.append(_QUOTE_PAIRS[ch])
        elif ch in CLOSE_QUOTES and stack:
            if ch in stack:
                while stack:
                    if stack.pop() == ch:
                        break
            if not stack:
                spans.append((outer_start, i + 1))
    return spans


def extract_unquoted_text(text: str) -> str:
    """Remove balanced quoted spans and normalize surrounding whitespace."""
    return " ".join(remove_quoted_spans(text).split())


def remove_quoted_spans(text: str) -> str:
    """Remove balanced quoted spans while preserving all outside characters."""
    pieces: list[str] = []
    cursor = 0
    for start, end in find_quote_spans(text):
        pieces.append(text[cursor:start])
        pieces.append(" ")
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def count_sentences(text: str) -> int:
    return len(split_sentence_units(text.strip())) if text.strip() else 0


def ends_with_question(sentence: str) -> bool:
    trimmed = sentence.rstrip().rstrip("".join(_TRAILING_MARKERS)).rstrip(_TRAILING_TERMINALS)
    return bool(trimmed and trimmed[-1] in _QUESTION_TERMINATORS)
