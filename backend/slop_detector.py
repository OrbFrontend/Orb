"""
slop_detector.py — Detect overused LLM phrases via word-level trigram fuzzy matching.

Usage:
    from slop_detector import detect_cliches

    SEED_PHRASE_BANK = [
        ["a mix of", "a mixture of"],
        ["tension in the air", "thick tension in the air"],
    ]

    result = detect_cliches(text, phrase_bank)
"""

import re
from dataclasses import dataclass, field

_N = 2
_DEFAULT_THRESHOLD = 0.25


@dataclass
class ClicheHit:
    canonical: str
    variant: str
    score: float


@dataclass
class FlaggedSentence:
    sentence: str
    cliches: list[ClicheHit] = field(default_factory=list)


@dataclass
class DetectionResult:
    flagged_sentences: list[FlaggedSentence]
    unique_cliches: list[str]
    total_sentences: int
    flagged_count: int


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def _trigrams(tokens: list[str]) -> set[tuple[str, ...]]:
    if len(tokens) < _N:
        return set()
    return {tuple(tokens[i : i + _N]) for i in range(len(tokens) - _N + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _split_sentences(text: str) -> list[str]:
    raw = re.split(r'(?<=[.!?"""\'])\s+', text.strip())
    return [s.strip() for s in raw if s.strip()]


def _match_sentence(
    sent_tokens: list[str],
    phrase_bank: list[list[str]],
    threshold: float,
) -> list[ClicheHit]:
    hits: list[ClicheHit] = []

    for variant_group in phrase_bank:
        best: ClicheHit | None = None
        best_score = 0.0

        for variant in variant_group:
            var_tokens = _tokenize(variant)
            var_grams = _trigrams(var_tokens)
            if not var_grams:
                continue

            window_len = len(var_tokens) + 3

            for start in range(max(1, len(sent_tokens) - window_len + 1)):
                window = sent_tokens[start : start + window_len]
                score = _jaccard(var_grams, _trigrams(window))

                if score >= threshold and score > best_score:
                    best_score = score
                    best = ClicheHit(
                        canonical=variant_group[0],
                        variant=variant,
                        score=round(score, 4),
                    )

        if best:
            hits.append(best)

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def detect_cliches(
    text: str,
    phrase_bank: list[list[str]],
    threshold: float = _DEFAULT_THRESHOLD,
) -> DetectionResult:
    sentences = _split_sentences(text)
    flagged: list[FlaggedSentence] = []
    all_canonicals: set[str] = set()

    for sentence in sentences:
        tokens = _tokenize(sentence)
        hits = _match_sentence(tokens, phrase_bank, threshold)
        if hits:
            flagged.append(FlaggedSentence(sentence=sentence, cliches=hits))
            all_canonicals.update(h.canonical for h in hits)

    return DetectionResult(
        flagged_sentences=flagged,
        unique_cliches=sorted(all_canonicals),
        total_sentences=len(sentences),
        flagged_count=len(flagged),
    )