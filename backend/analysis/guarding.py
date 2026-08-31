"""guarding.py — The protected-sequence guard for id-anchored editor patches.

:func:`analysis.patching.apply_id_patches` splices each replacement over the
exact offsets the audit recorded, so for document-ordered targets ``T1…Tn`` the
draft is ``P0 + T1 + P1 + … + Tn + Pn`` and the result is ``P0 + R1 + P1 + … +
Rn + Pn``. The ``T`` regions are mutable; every ``P`` region is protected, and
the back-to-front splice already guarantees each one survives byte-identically
and in order. One invariant is missing from that picture:

> A replacement ``R`` must not clone a significant contiguous sequence from a
> protected ``P`` region.

That is the defect behind the copies the editor kept producing — a replacement
for the narration between two lines of dialogue that hands back one of the
lines. Splicing it prints the protected text twice, and unlike the end-aligned
repeats :mod:`analysis.healing` trims, an interior copy cannot be removed with
any guarantee that the remainder rejoins. So this module only *rejects*; the
caller keeps the writer's original text for that target.

**Nothing here classifies prose.** Dialogue, action, and narration are not
distinguished, and no quote, asterisk, Markdown, or roleplay convention is
consulted. ``"Don't touch it."``, ``*Don't touch it.*``, ``Don’t touch it`` and
a bare ``Don't touch it`` all normalise to the lexical sequence ``don't / touch
/ it``, so a copy is caught whatever wrapper the model dressed it in — and a
wrapper change cannot be used to smuggle one past.

**The bands are strictly adjacent gaps.** Only ``P(k-1)`` and ``P(k)`` are
inspected for target ``k``. This is not a tuning choice: under back-to-front
application everything before ``T(k+1).start`` is still unpatched, so those two
gaps — and only those two — can be sliced out of the original draft and still
be the text that will surround the replacement. :func:`protected_bands` takes
its boundaries as neighbour offsets for exactly that reason; a gap two targets
away has already shifted by the net length change of the replacements to its
right, and slicing it with original offsets would read the wrong text silently.

**The policy is deliberately conservative and not yet calibrated.** ``No.``,
``Run.``, and ``Mara said.`` are ambiguous without content classification, and
a named entity is a legitimately repeated sequence, so the thresholds below
trade recall for false positives: a run must be long in both tokens and
characters, unique in the local protected context, and absent from the target's
own text. The first version accepts that it does not catch every clone. Matches
that clear the token floor but fail a later condition are logged and allowed
through, so the false-positive corpus can be read before the constants move.

It is not a prose-quality judge either. A paraphrase (``Mara warned Ilya not to
touch the door``), a swapped speaker, invented action, and lost meaning all pass
this guard untouched; those belong to eval criteria or a semantic review.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["Band", "ProtectedClone", "guard_protected_sequences", "protected_bands"]

# A clone must clear all three floors. Tokens alone would flag "she said to" and
# characters alone would flag one long word; the pair is what makes a match
# unlikely to be a coincidence of common prose.
MIN_CLONE_TOKENS = 3
MIN_CLONE_ALNUM = 10
# How much of each adjacent gap counts as local. Roughly a sentence or two on
# either side — far enough to cover the line a model reaches for, near enough
# that a repeat further out reads as the writer's own echo rather than a copy.
BAND_TOKENS = 32


# ── Lexical normalisation ─────────────────────────────────────────────────────


# Unicode word characters, underscore excluded so `_emphasis_` does not weld
# itself to the word beside it, with contractions kept whole.
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’ʼ][^\W_]+)*")
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'"})


@dataclass(frozen=True)
class _Token:
    """One lexical token: its comparison key and its span in the source text."""

    key: str
    start: int
    end: int


def _tokenize(text: str) -> list[_Token]:
    """Lexical tokens of *text*, case- and apostrophe-folded, with source offsets.

    Offsets are kept so a match can be quoted back in the draft's own spelling —
    the model is told which text it copied, not the folded key it collided on.
    Every character the regex skips (quotes, asterisks, dashes, punctuation) is
    formatting as far as this module is concerned.
    """
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


# ── Protected bands ───────────────────────────────────────────────────────────


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
    """The two protected gaps touching ``[start:end]``, trimmed to the local window.

    *previous_end* is where the previous target ended (0 for the first target)
    and *next_start* where the next one begins (``len(draft)`` for the last), so
    the gaps this returns are adjacent to the target by construction — the one
    property that lets them be sliced out of the original draft at all. Taking
    boundaries rather than gaps is what makes that structural: there is no
    argument that names a gap further away. Other targets are mutable and are
    excluded from both bands, since text that exists only inside another flagged
    span is not protected.

    The assertion pins the half a signature cannot: the two boundaries must
    bracket the target, or the "gaps" are not the text touching it. Offsets past
    the end of *draft* are left to Python's own slice clamping — a stale target
    list is the caller's bug, and it must not be this guard that raises on it.
    """
    assert 0 <= previous_end <= start <= end <= next_start, "protected bands must be the gaps strictly adjacent to the target"
    before_text = draft[previous_end:start]
    after_text = draft[end:next_start]
    before = _tokenize(before_text)[-BAND_TOKENS:]
    after = _tokenize(after_text)[:BAND_TOKENS]
    return (
        Band("before", before_text, tuple(before)),
        Band("after", after_text, tuple(after)),
    )


# ── Clone detection ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProtectedClone:
    """A run of protected text found inside a replacement, and why that is fatal."""

    text: str  # the run as the draft spells it
    side: str  # which adjacent gap it came from

    @property
    def rejection(self) -> str:
        """The reason in the caller's error vocabulary — no prefix, no full stop."""
        return (
            f"copies protected text from {self.side} the flagged span — “{self.text}” is "
            "already in the draft; replace only the flagged text"
        )


def _maximal_runs(keys: list[str], band: Band) -> list[tuple[int, int, int]]:
    """``(length, replacement index, band index)`` for every shared run, longest first.

    Only maximal runs are reported: a run that extends one token further left is
    the same copy seen from a worse starting point, and keeping both would make
    the ordering below depend on where the scan happened to start.
    """
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
    """Find a significant protected sequence cloned into *replacement*.

    *replacement* must be the **healed** text, not the model's raw ``replace``:
    healing trims the end-aligned copies it can safely rejoin, and guarding what
    it left is what keeps the two phases from disagreeing about the same run.

    Returns the longest run that clears every condition, or ``None``. Candidates
    are walked longest-first rather than only the single longest, so a legitimate
    long quote from the target's own text cannot mask a shorter real clone
    behind it.
    """
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
        # A sequence the target already contains may legitimately survive into
        # its own replacement; it is being edited, not copied from elsewhere.
        if _count_runs(target_keys, run):
            continue
        source = band.source(j, length)
        alnum = sum(1 for key in run for ch in key if ch.isalnum())
        occurrences = sum(_count_runs(side, run) for side in band_keys)
        if alnum < MIN_CLONE_ALNUM or occurrences != 1:
            # Medium confidence: long enough to notice, too short or too common
            # to reject while the false-positive rate is unmeasured.
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
