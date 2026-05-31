"""
slop_detector.py — Detect overused LLM phrases via literal matching or regex.

A phrase bank is a list of *groups*. Each group is one of two kinds:

* **literal** — a set of equivalent variant phrases. Short variants (≤3 tokens)
  match by exact (comma-insensitive) substring; longer variants (4+ tokens)
  match by trigram containment scoring.
* **regex** — a single regular expression evaluated against each sentence
  (case-insensitive). The matched text is reported verbatim. A pattern is only
  ever run against one sentence at a time, and a match that would span a
  sentence boundary (e.g. a greedy ``.*`` bridging two clauses across a ``.``)
  is rejected — so the phrase handed to the LLM is always contained to a single
  sentence.

Groups are accepted in two shapes for backwards compatibility:

    [                                     # phrase bank
        ["a mix of", "a mixture of"],     # legacy literal group (list of str)
        {"kind": "literal", "variants": ["tension in the air"]},
        {"kind": "regex", "pattern": r"the air (is|was) (thick|heavy|charged)"},
    ]

    result = detect_cliches(text, phrase_bank)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

from .text_segmentation import split_sentences

# A group is either a list of literal variants or a {kind, ...} dict.
PhraseGroup = Union[list[str], dict]

_N = 3
_EXACT_MATCH_MAX_LEN = 3
_DEFAULT_THRESHOLD = 0.4
_WINDOW_PADDING = 2

# A sentence-ending mark (optionally a closing quote) followed by either
# whitespace or a capital letter — the latter catches the no-space boundaries
# ("clear.The") that the sentence splitter leaves intact. Used to reject regex
# matches that bridge two sentences.
_SENTENCE_BOUNDARY = re.compile(r'[.!?]["”’\'*_)\]]*(\s|[A-Z])')


@dataclass
class ClicheHit:
    phrase: str
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


# Dialogue is intentionally kept: cliché matching must see text inside quotes.
_split_sentences = split_sentences


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
    """Normalise + pre-compile groups.

    Returns a list of ('literal', variants) or ('regex', compiled_pattern).
    Invalid regexes are skipped defensively so a single bad pattern can never
    abort the whole audit — the UI validates patterns before they are saved.
    """
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
    """Search a sentence with a compiled pattern; report the matched text.

    Matching is already scoped to a single sentence, but a greedy pattern can
    still bridge two sentences inside a chunk the splitter under-split (e.g. an
    abbreviation or ellipsis). Such matches are rejected so the reported phrase
    never spans a sentence boundary.
    """
    m = rx.search(sentence)
    if not m:
        return None
    matched = m.group(0).strip()
    if not matched or _SENTENCE_BOUNDARY.search(matched):
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

            # --- Short phrases: exact match (comma-insensitive) ---
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
                    best = ClicheHit(phrase=variant, score=round(score, 4))

        if best:
            hits.append(best)

    hits.sort(key=lambda h: h.score, reverse=True)
    return _deduplicate_hits(hits)


def _deduplicate_hits(hits: list[ClicheHit]) -> list[ClicheHit]:
    """Drop hits whose phrase tokens substantially overlap with a higher-scored hit.

    Prevents trigram-sharing phrases (e.g. "tension in the air" and
    "hanging in the air") from both firing when only one is in the text.
    """
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
