"""
slop_detector.py — Detect overused LLM phrases via exact + trigram containment matching.

Short phrases (≤3 tokens): exact substring match.
Longer phrases (4+ tokens): trigram containment scoring.

Usage:
    from slop_detector import detect_cliches

    SEED_PHRASE_BANK = [
        ["a mix of", "a mixture of"],
        ["tension in the air", "the air is thick"],
    ]

    result = detect_cliches(text, SEED_PHRASE_BANK)
"""

import re
from dataclasses import dataclass, field

_N = 3
_EXACT_MATCH_MAX_LEN = 3
_DEFAULT_THRESHOLD = 0.4
_WINDOW_PADDING = 2


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


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _containment(phrase_grams: set, window_grams: set) -> float:
    """Fraction of phrase n-grams present in the window."""
    if not phrase_grams:
        return 0.0
    return len(phrase_grams & window_grams) / len(phrase_grams)


def _split_sentences(text: str) -> list[str]:
    raw = re.split(r'(?<=[.!?])["”]?\s+|\n+', text.strip())
    return [s.strip() for s in raw if s.strip()]


def _match_sentence(
    sent_tokens: list[str],
    sent_lower: str,
    phrase_bank: list[list[str]],
    threshold: float,
) -> list[ClicheHit]:
    hits: list[ClicheHit] = []
    # Precompute normalised sentence for comma-insensitive short matches
    sent_normalised = " ".join(sent_tokens)

    for variant_group in phrase_bank:
        best: ClicheHit | None = None
        best_score = 0.0

        for variant in variant_group:
            var_tokens = _tokenize(variant)

            # --- Short phrases: exact match (comma-insensitive) ---
            if len(var_tokens) <= _EXACT_MATCH_MAX_LEN:
                if len(var_tokens) == 1:
                    # Single word: word-boundary check to avoid substrings
                    pattern = rf"\b{re.escape(variant)}\b"
                    if re.search(pattern, sent_lower) and 1.0 > best_score:
                        best_score = 1.0
                        best = ClicheHit(
                            canonical=variant_group[0],
                            variant=variant,
                            score=1.0,
                        )
                else:
                    # 2–3 tokens: compare normalised forms (strips commas)
                    normalised_variant = " ".join(var_tokens)
                    if normalised_variant in sent_normalised and 1.0 > best_score:
                        best_score = 1.0
                        best = ClicheHit(
                            canonical=variant_group[0],
                            variant=variant,
                            score=1.0,
                        )
                continue

            # --- Longer phrases: trigram containment ---
            var_grams = _ngrams(var_tokens, _N)
            if not var_grams:
                continue

            window_len = min(len(var_tokens) + _WINDOW_PADDING, len(sent_tokens))

            for start in range(len(sent_tokens) - window_len + 1):
                window = sent_tokens[start : start + window_len]
                win_grams = _ngrams(window, _N)
                score = _containment(var_grams, win_grams)

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
        sent_lower = sentence.lower()
        hits = _match_sentence(tokens, sent_lower, phrase_bank, threshold)
        if hits:
            flagged.append(FlaggedSentence(sentence=sentence, cliches=hits))
            all_canonicals.update(h.canonical for h in hits)

    return DetectionResult(
        flagged_sentences=flagged,
        unique_cliches=sorted(all_canonicals),
        total_sentences=len(sentences),
        flagged_count=len(flagged),
    )
