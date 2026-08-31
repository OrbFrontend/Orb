"""Detect overused phrases against the configured phrase bank."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..text.lexical import ngrams
from ..text.text_segmentation import split_segment_sentences

if TYPE_CHECKING:
    from ...database.models import PhraseGroup

_N = 3
_EXACT_MATCH_MAX_LEN = 3
_DEFAULT_THRESHOLD = 0.4
_WINDOW_PADDING = 2


@dataclass(slots=True)
class ClicheHit:
    phrase: str
    score: float


@dataclass(slots=True)
class FlaggedSentence:
    sentence: str
    cliches: list[ClicheHit] = field(default_factory=list)


@dataclass(slots=True)
class DetectionResult:
    flagged_sentences: list[FlaggedSentence]
    unique_cliches: list[str]
    total_sentences: int
    flagged_count: int


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def _gram_set(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    """Distinct n-grams of tokens, for set-containment scoring."""
    return set(ngrams(tokens, n))


def _containment(phrase_grams: set, window_grams: set) -> float:
    """Fraction of the phrase's n-grams that appear in the window."""
    if not phrase_grams:
        return 0.0
    return len(phrase_grams & window_grams) / len(phrase_grams)


_split_sentences = split_segment_sentences


def _group_kind(group: PhraseGroup) -> str:
    """Return 'regex' or 'literal' for a phrase-bank group of either shape."""
    if isinstance(group, dict):
        return "regex" if group.get("kind") == "regex" else "literal"
    return "literal"


def _group_variants(group: PhraseGroup) -> list[str]:
    """Literal variants for a group (empty for regex groups)."""
    if isinstance(group, dict):
        return [v for v in (group.get("variants") or []) if isinstance(v, str)]
    return [v for v in group if isinstance(v, str)]


def _group_pattern(group: PhraseGroup) -> str:
    """Regex pattern string for a group ('' when not a regex group)."""
    if isinstance(group, dict):
        pat = group.get("pattern")
        return pat if isinstance(pat, str) else ""
    return ""


def _compile_phrase_bank(phrase_bank: list[PhraseGroup]) -> list[tuple]:
    """Normalize and compile phrase-bank groups."""
    compiled: list[tuple] = []
    for group in phrase_bank:
        if _group_kind(group) == "regex":
            pattern = _group_pattern(group).strip()
            if not pattern:
                continue
            try:
                compiled.append(("regex", re.compile(pattern, re.IGNORECASE)))
            except re.error:
                continue
        else:
            variants = _group_variants(group)
            if variants:
                compiled.append(("literal", variants))
    return compiled


def _match_regex_group(rx: re.Pattern, sentence: str) -> ClicheHit | None:
    """Return a regex hit in one sentence, if present."""
    m = rx.search(sentence)
    if not m:
        return None
    matched = m.group(0).strip()
    if not matched:
        return None
    return ClicheHit(phrase=matched, score=1.0)


def _match_sentence(
    sent_tokens: list[str],
    sent_lower: str,
    sentence: str,
    compiled_groups: list[tuple],
    threshold: float,
) -> list[ClicheHit]:
    hits: list[ClicheHit] = []
    # Precompute normalised sentence for comma-insensitive short matches
    sent_normalised = " ".join(sent_tokens)

    for kind, payload in compiled_groups:
        if kind == "regex":
            hit = _match_regex_group(payload, sentence)
            if hit:
                hits.append(hit)
            continue

        variant_group = payload
        best: ClicheHit | None = None
        best_score = 0.0

        for variant in variant_group:
            var_tokens = _tokenize(variant)

            if len(var_tokens) <= _EXACT_MATCH_MAX_LEN:
                if len(var_tokens) == 1:
                    # Single word: word-boundary check to avoid substrings
                    pattern = rf"\b{re.escape(variant)}\b"
                    if re.search(pattern, sent_lower) and 1.0 > best_score:
                        best_score = 1.0
                        best = ClicheHit(phrase=variant, score=1.0)
                else:
                    # 2–3 tokens: compare normalised forms (strips commas)
                    normalised_variant = " ".join(var_tokens)
                    if normalised_variant in sent_normalised and 1.0 > best_score:
                        best_score = 1.0
                        best = ClicheHit(phrase=variant, score=1.0)
                continue

            var_grams = _gram_set(var_tokens, _N)
            if not var_grams:
                continue

            window_len = min(len(var_tokens) + _WINDOW_PADDING, len(sent_tokens))

            for start in range(len(sent_tokens) - window_len + 1):
                window = sent_tokens[start : start + window_len]
                win_grams = _gram_set(window, _N)
                score = _containment(var_grams, win_grams)

                if score >= threshold and score > best_score:
                    best_score = score
                    best = ClicheHit(phrase=variant, score=round(score, 4))

        if best:
            hits.append(best)

    hits.sort(key=lambda h: h.score, reverse=True)
    return _deduplicate_hits(hits)


def _deduplicate_hits(hits: list[ClicheHit]) -> list[ClicheHit]:
    """Drop lower-scored hits that substantially overlap a better hit."""
    if len(hits) <= 1:
        return hits
    kept: list[ClicheHit] = []
    for hit in hits:
        hit_toks = set(_tokenize(hit.phrase))
        dominated = any(
            len(hit_toks & set(_tokenize(better.phrase))) / len(hit_toks | set(_tokenize(better.phrase))) >= 0.5
            for better in kept
        )
        if not dominated:
            kept.append(hit)
    return kept


def detect_cliches(
    text: str,
    phrase_bank: list[PhraseGroup],
    threshold: float = _DEFAULT_THRESHOLD,
) -> DetectionResult:
    sentences = _split_sentences(text)
    compiled_groups = _compile_phrase_bank(phrase_bank)
    flagged: list[FlaggedSentence] = []
    all_phrases: set[str] = set()

    for sentence in sentences:
        tokens = _tokenize(sentence)
        sent_lower = sentence.lower()
        hits = _match_sentence(tokens, sent_lower, sentence, compiled_groups, threshold)
        if hits:
            flagged.append(FlaggedSentence(sentence=sentence, cliches=hits))
            all_phrases.update(h.phrase for h in hits)

    return DetectionResult(
        flagged_sentences=flagged,
        unique_cliches=sorted(all_phrases),
        total_sentences=len(sentences),
        flagged_count=len(flagged),
    )
