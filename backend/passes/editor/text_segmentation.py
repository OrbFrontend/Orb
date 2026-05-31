"""
text_segmentation.py — Shared paragraph / sentence / dialogue segmentation for
the editor audit passes.

Every scanner (opening_monotony, template_repetition, phrase_repetition,
slop_detector, contrastive_negation, structural_repetition) used to carry its
own copy of these regexes and helpers, and they had drifted apart: some treated
``…`` as a sentence terminator and some didn't; some recognised single curly
quotes and some didn't; one lacked the trailing-marker tolerance the others had.
This module is the single source of truth so the passes segment text the same
way.

Two families of consumers, served by two functions:

* ``split_sentences`` keeps dialogue intact — for scanners that match inside
  quotes (slop_detector) or analyse clause grammar (contrastive_negation).
* ``split_narration_sentences`` strips dialogue first — for scanners that only
  care about narration prose (opening_monotony, template_repetition,
  phrase_repetition).

``find_quote_spans`` and ``count_sentences`` support structural_repetition,
which classifies blocks rather than stripping them but still wants the same
quote/terminator definitions.
"""

from __future__ import annotations

import re

__all__ = [
    "PARA_SPLIT",
    "SENT_SPLIT",
    "OPEN_QUOTES",
    "CLOSE_QUOTES",
    "TOGGLE_QUOTES",
    "split_paragraphs",
    "split_sentences",
    "extract_narration",
    "split_narration_sentences",
    "find_quote_spans",
    "count_sentences",
]


# ---------- canonical patterns ----------

# Paragraph break: a blank line, optionally filled with whitespace.
PARA_SPLIT = re.compile(r"\n\s*\n")

# Sentence boundary: a terminator (``. ! ? …``) followed by whitespace. We
# tolerate trailing closing markers between the terminator and the whitespace —
# quotes and markdown emphasis/brackets (e.g. ``Hiro.*`` or ``done."``).
# Without this, a terminator hidden behind a markdown marker fails to split,
# merging adjacent sentences (and, across newlines, whole paragraphs) into one
# unit.
SENT_SPLIT = re.compile(r"(?<=[.!?…])[\"”’'*_)\]]*\s+")

# Curly directional quotes are unambiguous: left opens, right closes.
OPEN_QUOTES = frozenset({"“", "‘"})  # “ ‘
CLOSE_QUOTES = frozenset({"”", "’"})  # ” ’
# Straight double quote has no direction; we toggle on each occurrence.
# The straight single quote is deliberately excluded from every set so that
# contractions (I'm, don't) survive. (Note: U+2019 doubles as a typographic
# apostrophe, so curly-apostrophe contractions can still be clipped — an
# accepted trade-off for recognising single-quoted dialogue.)
TOGGLE_QUOTES = frozenset({'"'})


# ---------- paragraph / sentence splitting (dialogue preserved) ----------


def split_paragraphs(text: str) -> list[str]:
    """Split on blank-line paragraph breaks, dropping empty paragraphs."""
    return [p for p in PARA_SPLIT.split(text.strip()) if p.strip()]


def split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences, paragraph-first, keeping dialogue intact.

    Paragraph-first splitting means a paragraph whose final sentence lacks a
    detectable terminator can't merge into the next paragraph.
    """
    sentences: list[str] = []
    for para in split_paragraphs(text):
        for raw in SENT_SPLIT.split(para):
            s = raw.strip()
            if s:
                sentences.append(s)
    return sentences


# ---------- dialogue stripping ----------


def extract_narration(paragraph: str) -> str:
    """Return only the characters of ``paragraph`` that lie outside any quote.

    The caller splits into paragraphs first, so state resets at each paragraph
    boundary and an unclosed quote inside one paragraph cannot contaminate the
    next. Spaces are inserted where quotes are stripped, to prevent word fusion.
    """
    out: list[str] = []
    inside = False
    prev_was_quote = False

    for ch in paragraph:
        if ch in TOGGLE_QUOTES:
            inside = not inside
            prev_was_quote = True
            continue
        if ch in OPEN_QUOTES:
            inside = True
            prev_was_quote = True
            continue
        if ch in CLOSE_QUOTES:
            inside = False
            prev_was_quote = True
            continue

        if not inside:
            # Insert a space where a quote was stripped, to prevent fusion.
            if prev_was_quote and out and out[-1] not in " \t\n":
                out.append(" ")
            out.append(ch)
        else:
            # Inside quotes — still guard against fusion at the boundary.
            if out and out[-1] not in " \t\n":
                out.append(" ")

        prev_was_quote = False

    return " ".join("".join(out).split())  # Normalize whitespace


def split_narration_sentences(text: str) -> list[str]:
    """Paragraph-aware sentence splitter that strips dialogue in the process."""
    sentences: list[str] = []
    for para in split_paragraphs(text):
        narration = extract_narration(para).strip()
        if not narration:
            continue
        for raw in SENT_SPLIT.split(narration):
            s = raw.strip()
            if s:
                sentences.append(s)
    return sentences


# ---------- structural helpers ----------


def find_quote_spans(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` spans of quoted regions, inclusive of the quotes.

    Used by block-classifying consumers that need quote *positions* rather than
    stripped narration. Shares the same quote definitions as
    ``extract_narration`` so the two agree on what counts as dialogue.
    """
    spans: list[tuple[int, int]] = []
    inside = False
    start = 0
    for i, ch in enumerate(text):
        if ch in TOGGLE_QUOTES:
            if not inside:
                inside = True
                start = i
            else:
                spans.append((start, i + 1))
                inside = False
        elif ch in OPEN_QUOTES:
            if not inside:
                inside = True
                start = i
        elif ch in CLOSE_QUOTES:
            if inside:
                spans.append((start, i + 1))
                inside = False
    return spans


def count_sentences(text: str) -> int:
    """Count sentences in a block of text.

    Non-empty text with no sentence terminator still counts as 1 (a fragment or
    short imperative). Empty text returns 0.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    pieces = [s.strip() for s in SENT_SPLIT.split(stripped) if s.strip()]
    return len(pieces) if pieces else 1
