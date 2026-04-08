"""
opening_monotony.py — Detect repetitive sentence openings in LLM output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class FlaggedOpener:
    opener: str
    count: int
    fraction: float
    sentences: list[str] = field(default_factory=list)


@dataclass
class MonotonyResult:
    flagged_openers: list[FlaggedOpener]
    all_openers: dict[str, int]
    total_sentences: int
    monotony_score: float


def _split_sentences(text: str) -> list[str]:
    raw = re.split(r'(?<=[.!?"""\'])\s+', text.strip())
    return [s.strip() for s in raw if s.strip()]


def _normalize(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _get_opener(sentence: str, n_words: int) -> str | None:
    words = sentence.split()
    if len(words) < n_words:
        return None
    return " ".join(_normalize(w) for w in words[:n_words])


def detect_opening_monotony(
    text: str,
    n_words: int = 3,
    flag_threshold: float = 0.15,
) -> MonotonyResult:
    sentences = _split_sentences(text)
    total = len(sentences)
    if total == 0:
        return MonotonyResult([], {}, 0, 0.0)

    opener_sentences: dict[str, list[str]] = {}
    for sent in sentences:
        opener = _get_opener(sent, n_words)
        if opener:
            opener_sentences.setdefault(opener, []).append(sent)

    counts = {k: len(v) for k, v in opener_sentences.items()}

    flagged: list[FlaggedOpener] = []
    for opener, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        frac = count / total
        if frac >= flag_threshold and count >= 2:
            flagged.append(FlaggedOpener(
                opener=opener,
                count=count,
                fraction=round(frac, 4),
                sentences=opener_sentences[opener],
            ))

    repeated_count = sum(c for c in counts.values() if c >= 2)
    monotony_score = round(repeated_count / total, 4) if total else 0.0

    return MonotonyResult(
        flagged_openers=flagged,
        all_openers=counts,
        total_sentences=total,
        monotony_score=monotony_score,
    )