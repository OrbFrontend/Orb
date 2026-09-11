"""Trim exact context copies from editor replacements."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .audit import _OUTER_MARKERS
from .text.text_segmentation import HARD_LINE_BREAK_RE, PARA_SPLIT, extract_block_spans

__all__ = ["HealedPatch", "heal_replacement"]


@dataclass(frozen=True)
class HealedPatch:
    """A replacement ready to splice, or a rejection reason."""

    start: int
    end: int
    replace: str
    notes: tuple[str, ...] = ()
    rejection: str | None = None


_WORD_RE = re.compile(r"\S+")


def _word_spans(text: str) -> list[tuple[int, int]]:
    """Return whitespace-delimited word offsets."""
    return [match.span() for match in _WORD_RE.finditer(text)]


def _key(word: str) -> str:
    """Return the case-folded comparison key for a word."""
    return word.casefold()


def _neighbour_keys(text: str) -> list[str]:
    """Return comparison keys for non-punctuation neighbour words."""
    return [_key(word) for word in _WORD_RE.findall(text) if any(ch.isalnum() for ch in word)]


def _tail_repeat(keys: Sequence[str], following: Sequence[str]) -> int:
    """Return the trailing replacement words repeated after the span."""
    for k in range(min(len(keys), len(following)), 0, -1):
        if list(keys[-k:]) == list(following[:k]):
            return k
    return 0


def _head_repeat(keys: Sequence[str], preceding: Sequence[str]) -> int:
    """How many leading words of the replacement repeat the draft behind it."""
    for k in range(min(len(keys), len(preceding)), 0, -1):
        if list(keys[:k]) == list(preceding[-k:]):
            return k
    return 0


# Target spans are marker-stripped (``audit._strip_markers``), so the quotes or
# emphasis around flagged text stay in the draft on either side of the span.
# Straight and curly double quotes are one mark; a curly apostrophe stays apart
# from them so ``sayin’`` never reads as a closing quote.
_MARKER_KIND = str.maketrans({"“": '"', "”": '"', "’": "‘"})


def _leading_markers(text: str) -> str:
    """Return the quote/emphasis markers that open *text*."""
    return text[: len(text) - len(text.lstrip(_OUTER_MARKERS))]


def _trailing_markers(text: str) -> str:
    """Return the quote/emphasis markers that close *text*."""
    return text[len(text.rstrip(_OUTER_MARKERS)) :]


def _enclosing_block(draft: str, start: int, end: int) -> tuple[int, int] | None:
    """Return the speech or emphasis block holding ``draft[start:end]``, if any."""
    para_start = 0
    for match in PARA_SPLIT.finditer(draft, 0, start):
        para_start = match.end()
    next_break = PARA_SPLIT.search(draft, end)
    para_end = next_break.start() if next_break else len(draft)
    for typ, block_start, block_end in extract_block_spans(draft[para_start:para_end]):
        if typ != "NARRATION" and para_start + block_start < start and end < para_start + block_end:
            return para_start + block_start, para_start + block_end
    return None


def _wrapping_markers(draft: str, start: int, end: int) -> tuple[str, str]:
    """Return the markers the draft already has before and after the span.

    Each side is the run touching the span, plus the enclosing block's own
    delimiter when the span sits mid-block and that delimiter is further off.
    """
    before = _trailing_markers(draft[:start])
    after = _leading_markers(draft[end:])
    block = _enclosing_block(draft, start, end)
    if block is not None:
        block_start, block_end = block
        if start - len(before) > block_start:
            before = _leading_markers(draft[block_start:start]) + before
        if end + len(after) < block_end:
            after += _trailing_markers(draft[end:block_end])
    return before, after


def _trim_wrapping_markers(draft: str, start: int, end: int, text: str) -> tuple[str, list[str]]:
    """Drop the outer markers of *text* that the draft already wraps around the span."""
    before, after = _wrapping_markers(draft, start, end)
    notes: list[str] = []
    opening = _head_repeat(_leading_markers(text).translate(_MARKER_KIND), before.translate(_MARKER_KIND))
    if opening:
        text = text[opening:].lstrip()
        notes.append(f"dropped {opening} opening marker(s) the draft already has before the span")
    closing = _tail_repeat(_trailing_markers(text).translate(_MARKER_KIND), after.translate(_MARKER_KIND))
    if closing:
        text = text[:-closing].rstrip()
        notes.append(f"dropped {closing} closing marker(s) the draft already has after the span")
    return text, notes


_SEPARATORS = ("", " ", "\n", "\n\n")


def _separator_strength(run: str) -> int:
    """Index into :data:`_SEPARATORS` for the break a whitespace run represents."""
    if PARA_SPLIT.search(run):
        return 3
    if HARD_LINE_BREAK_RE.search(run):
        return 2
    return 1 if run else 0


def _collapse_deletion_seam(draft: str, start: int, end: int) -> tuple[int, int, str]:
    """Close the whitespace seam left by an empty replacement."""
    lead = start
    while lead > 0 and draft[lead - 1].isspace():
        lead -= 1
    trail = end
    while trail < len(draft) and draft[trail].isspace():
        trail += 1
    if lead == 0 or trail == len(draft):
        return lead, trail, ""
    strength = max(_separator_strength(draft[lead:start]), _separator_strength(draft[end:trail]))
    return lead, trail, _SEPARATORS[strength]


def heal_replacement(draft: str, start: int, end: int, replace: str) -> HealedPatch:
    """Trim repeated context from one replacement."""
    text = replace.strip()
    spans = _word_spans(text)
    keys = [_key(text[s:e]) for s, e in spans]
    notes: list[str] = []
    lo, hi = 0, len(spans)

    trailing = _tail_repeat(keys, _neighbour_keys(draft[end:]))
    if trailing:
        hi -= trailing
        notes.append(f"trimmed {trailing} trailing word(s) copied from the draft after the span")
    leading = _head_repeat(keys[lo:hi], _neighbour_keys(draft[:start]))
    if leading:
        lo += leading
        notes.append(f"trimmed {leading} leading word(s) copied from the draft before the span")
    if lo or hi < len(spans):
        text = text[spans[lo][0] : spans[hi - 1][1]].strip() if lo < hi else ""
    text, marker_notes = _trim_wrapping_markers(draft, start, end, text)
    notes.extend(marker_notes)

    if text:
        return HealedPatch(start, end, text, tuple(notes))

    if replace.strip():
        return HealedPatch(
            start,
            end,
            replace,
            tuple(notes),
            rejection="only repeats text that already surrounds the flagged span — send new prose for the flagged text itself",
        )

    new_start, new_end, separator = _collapse_deletion_seam(draft, start, end)
    if (new_start, new_end) != (start, end):
        notes.append("closed the whitespace gap the deletion left")
    return HealedPatch(new_start, new_end, separator, tuple(notes))
