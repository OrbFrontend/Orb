"""Reject editor patches that clone text from adjacent protected spans."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["Band", "ProtectedClone", "guard_protected_sequences", "protected_bands"]

# Require enough tokens and characters to avoid common matches.
MIN_CLONE_TOKENS = 3
MIN_CLONE_ALNUM = 10
# Inspect only a local window around each target.
BAND_TOKENS = 32


_TOKEN_RE = re.compile(r"[^\W_]+(?:['’ʼ][^\W_]+)*")
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'"})


@dataclass(frozen=True)
class _Token:
    """One lexical token: its comparison key and its span in the source text."""

    key: str
    start: int
    end: int


def _tokenize(text: str) -> list[_Token]:
    """Return folded tokens with source offsets."""
    return [
        _Token(match.group(0).translate(_APOSTROPHES).casefold(), match.start(), match.end())
        for match in _TOKEN_RE.finditer(text)
    ]


def _keys(text: str) -> list[str]:
    return [token.key for token in _tokenize(text)]


def _count_runs(keys: list[str], run: list[str]) -> int:
    """How many times the token sequence *run* occurs in *keys*."""
    n = len(run)
    return sum(1 for i in range(len(keys) - n + 1) if keys[i : i + n] == run)


@dataclass(frozen=True)
class Band:
    """The local window of one adjacent protected gap."""

    side: str  # "before" or "after", as it reads in the rejection message
    text: str
    tokens: tuple[_Token, ...]

    def source(self, index: int, length: int) -> str:
        """The draft's own text for *length* tokens starting at *index*."""
        return self.text[self.tokens[index].start : self.tokens[index + length - 1].end]


def protected_bands(draft: str, previous_end: int, start: int, end: int, next_start: int) -> tuple[Band, Band]:
    """Return the two adjacent protected windows for a target."""
    assert 0 <= previous_end <= start <= end <= next_start, "protected bands must be the gaps strictly adjacent to the target"
    before_text = draft[previous_end:start]
    after_text = draft[end:next_start]
    before = _tokenize(before_text)[-BAND_TOKENS:]
    after = _tokenize(after_text)[:BAND_TOKENS]
    return (
        Band("before", before_text, tuple(before)),
        Band("after", after_text, tuple(after)),
    )


@dataclass(frozen=True)
class ProtectedClone:
    """A protected run found inside a replacement."""

    text: str  # the run as the draft spells it
    side: str  # which adjacent gap it came from

    @property
    def rejection(self) -> str:
        """Return the caller-facing rejection text."""
        return (
            f"copies protected text from {self.side} the flagged span — “{self.text}” is "
            "already in the draft; replace only the flagged text"
        )


def _maximal_runs(keys: list[str], band: Band) -> list[tuple[int, int, int]]:
    """Return maximal shared token runs."""
    band_keys = [token.key for token in band.tokens]
    runs: list[tuple[int, int, int]] = []
    for i, key in enumerate(keys):
        for j, band_key in enumerate(band_keys):
            if key != band_key or (i and j and keys[i - 1] == band_keys[j - 1]):
                continue
            length = 0
            while i + length < len(keys) and j + length < len(band_keys) and keys[i + length] == band_keys[j + length]:
                length += 1
            runs.append((length, i, j))
    return runs


def guard_protected_sequences(replacement: str, bands: tuple[Band, Band], target_span: str) -> ProtectedClone | None:
    """Return a significant protected run copied into *replacement*, if any."""
    keys = _keys(replacement)
    if len(keys) < MIN_CLONE_TOKENS:
        return None
    target_keys = _keys(target_span)
    band_keys = [[token.key for token in band.tokens] for band in bands]

    candidates = [(length, i, j, band) for band in bands for length, i, j in _maximal_runs(keys, band)]
    for length, i, j, band in sorted(candidates, key=lambda c: (-c[0], c[1], c[3].side)):
        if length < MIN_CLONE_TOKENS:
            break
        run = keys[i : i + length]
        if _count_runs(target_keys, run):
            continue
        source = band.source(j, length)
        alnum = sum(1 for key in run for ch in key if ch.isalnum())
        occurrences = sum(_count_runs(side, run) for side in band_keys)
        if alnum < MIN_CLONE_ALNUM or occurrences != 1:
            logger.info(
                "Protected-sequence guard: allowing a %d-token match %r (%d alnum chars, %d local occurrence(s))",
                length,
                source,
                alnum,
                occurrences,
            )
            continue
        return ProtectedClone(source, band.side)
    return None
