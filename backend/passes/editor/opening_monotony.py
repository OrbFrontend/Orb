"""
opening_monotony.py — Detect repetitive sentence openings in LLM output.

Public API (unchanged from the original):
    detect_opening_monotony(text, n_words=1, min_consecutive=3) -> MonotonyResult
    MonotonyResult, FlaggedOpener  (dataclasses)

Internals rewritten:
    - Dialogue stripping is now paragraph-first with a stateful quote scanner.
    - Sentence splitting happens per-paragraph, after stripping.
    This fixes paragraph-spanning dialogue, unclosed/truncated quotes, and
    the `...` + quote boundary case that broke the previous regex approach.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

DEBUG = "DEBUG_OPENING_MONOTONY" in os.environ
# ---------- public dataclasses (unchanged) ----------


@dataclass
class FlaggedOpener:
    opener: str
    count: int
    max_run: int
    fraction: float
    sentences: list[str] = field(default_factory=list)


@dataclass
class MonotonyResult:
    flagged_openers: list[FlaggedOpener]
    all_openers: dict[str, int]
    total_sentences: int
    monotony_score: float


# ---------- narration extraction ----------

_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENT_SPLIT = re.compile(r"(?<=[.!?\u2026])[\"\u201d]?\s+")

# Curly directional quotes are unambiguous: left opens, right closes.
_OPEN_QUOTES = {"\u201c"}  # "
_CLOSE_QUOTES = {"\u201d"}  # "
# Straight double quote has no direction; we toggle on each occurrence.
# Note: Single quote is excluded to avoid issues with contractions like I'm, don't, etc.
_TOGGLE_QUOTES = {'"'}


def _extract_narration(paragraph: str) -> str:
    """Return only the characters of `paragraph` that lie outside any quote.

    State resets at the start of each paragraph (the caller splits first), so
    an unclosed quote inside one paragraph cannot contaminate later ones.

    Inserts spaces when stripping quotes to prevent word fusion.
    """
    out: list[str] = []
    inside = False
    prev_was_quote = False

    for ch in paragraph:
        if ch in _TOGGLE_QUOTES:
            inside = not inside
            prev_was_quote = True
            continue
        if ch in _OPEN_QUOTES:
            inside = True
            prev_was_quote = True
            continue
        if ch in _CLOSE_QUOTES:
            inside = False
            prev_was_quote = True
            continue

        if not inside:
            # Insert space where quote was stripped to prevent fusion
            if prev_was_quote and out and out[-1] not in " \t\n":
                out.append(" ")
            out.append(ch)
        else:
            # Inside quotes - still insert space to prevent fusion at boundary
            if out and out[-1] not in " \t\n":
                out.append(" ")

        prev_was_quote = False

    return " ".join("".join(out).split())  # Normalize whitespace


def _split_sentences(text: str) -> list[str]:
    """Paragraph-aware sentence splitter that strips dialogue in the process."""
    if DEBUG:
        sys.stderr.write(f"[opening_monotony] splitting text: {repr(text)}\n")
    sentences: list[str] = []
    for para in _PARA_SPLIT.split(text.strip()):
        narration = _extract_narration(para).strip()
        if DEBUG:
            sys.stderr.write(f"[opening_monotony] para: {repr(para)}\n")
            sys.stderr.write(f"[opening_monotony] narration: {repr(narration)}\n")
        if not narration:
            continue
        for raw in _SENT_SPLIT.split(narration):
            s = raw.strip()
            if s:
                sentences.append(s)
    if DEBUG:
        sys.stderr.write(f"[opening_monotony] extracted sentences: {sentences}\n")
    return sentences


# ---------- opener analysis (unchanged logic) ----------


def _normalize(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _get_opener(sentence: str, n_words: int) -> str | None:
    words = sentence.split()
    if len(words) < n_words:
        return None
    normalized = [_normalize(w) for w in words[:n_words]]
    if any(w == "" for w in normalized):
        return None
    return " ".join(normalized)


def detect_opening_monotony(
    text: str,
    n_words: int = 1,
    min_consecutive: int = 3,
) -> MonotonyResult:
    sentences = _split_sentences(text)
    if DEBUG:
        sys.stderr.write(f"[opening_monotony] sentences: {sentences}\n")
    total = len(sentences)
    if total == 0:
        return MonotonyResult([], {}, 0, 0.0)

    openers: list[str | None] = [_get_opener(s, n_words) for s in sentences]
    if DEBUG:
        sys.stderr.write(f"[opening_monotony] openers: {openers}\n")

    counts: dict[str, int] = {}
    for opener in openers:
        if opener:
            counts[opener] = counts.get(opener, 0) + 1

    # Longest consecutive run per opener, and the sentences in that run.
    max_runs: dict[str, int] = {}
    run_sentences: dict[str, list[str]] = {}

    current_opener: str | None = None
    current_run: list[str] = []

    def _flush():
        if current_opener and len(current_run) > max_runs.get(current_opener, 0):
            max_runs[current_opener] = len(current_run)
            run_sentences[current_opener] = list(current_run)

    for sent, opener in zip(sentences, openers):
        if opener and opener == current_opener:
            current_run.append(sent)
        else:
            _flush()
            current_opener = opener
            current_run = [sent] if opener else []
    _flush()

    flagged: list[FlaggedOpener] = []
    for opener, max_run in sorted(max_runs.items(), key=lambda x: x[1], reverse=True):
        if max_run >= min_consecutive:
            count = counts[opener]
            flagged.append(
                FlaggedOpener(
                    opener=opener,
                    count=count,
                    max_run=max_run,
                    fraction=round(count / total, 4),
                    sentences=run_sentences[opener],
                )
            )

    repeated_count = sum(c for c in counts.values() if c >= 2)
    monotony_score = round(repeated_count / total, 4) if total else 0.0

    return MonotonyResult(
        flagged_openers=flagged,
        all_openers=counts,
        total_sentences=total,
        monotony_score=monotony_score,
    )
