"""Trim exact context copies from editor replacements."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .text.text_segmentation import HARD_LINE_BREAK_RE, PARA_SPLIT

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
