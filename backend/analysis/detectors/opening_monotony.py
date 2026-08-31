"""Detect repetitive openings in narration sentences."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from ..text.lexical import normalize_word
from ..text.text_segmentation import split_narration_sentences

DEBUG = "DEBUG_OPENING_MONOTONY" in os.environ


@dataclass(slots=True)
class FlaggedOpener:
    opener: str
    count: int
    max_run: int
    fraction: float
    sentences: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MonotonyResult:
    flagged_openers: list[FlaggedOpener]
    all_openers: dict[str, int]
    total_sentences: int
    monotony_score: float


_split_sentences = split_narration_sentences


def _get_opener(sentence: str, n_words: int) -> str | None:
    words = sentence.split()
    if len(words) < n_words:
        return None
    normalized = [normalize_word(w) for w in words[:n_words]]
    if any(w == "" for w in normalized):
        return None
    return " ".join(normalized)


def detect_opening_monotony(
    text: str,
    n_words: int = 1,
    min_consecutive: int = 4,
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
