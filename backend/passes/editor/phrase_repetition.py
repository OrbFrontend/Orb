"""
phrase_repetition.py — Detect exact (n-gram) phrase repetition across messages.

Public API:
    detect_phrase_repetition(messages, min_n=3, max_n=5, min_messages=2,
                             min_content_words=2, require_last_message=True)
    PhraseResult, FlaggedPhrase  (dataclasses)

Logic:
    - For each message: strip dialogue, split into sentences, tokenize.
    - Extract every n-gram (n in [min_n, max_n]) and record the set of
      *distinct* message indices it appears in (in-message duplicates do
      not inflate the count).
    - Filter out n-grams whose content-word count (non-stopword tokens) is
      below `min_content_words` — kills "in the air", "I don't know", etc.
    - Filter out n-grams appearing in fewer than `min_messages` messages.
    - Suppress sub-n-grams: when a shorter n-gram is contained in a longer
      one *and they appear in exactly the same set of messages*, drop the
      shorter one — it adds no information.
    - When `require_last_message` is True, only keep n-grams that appear in
      the final message (the current draft). This makes the report
      actionable for the message currently being audited.

Example target pattern:
    Message 1: "His shadowed red eyes flickered in the firelight."
    Message 4: "She met the shadowed red eyes across the table."
    Message 7: "Behind the mask, his shadowed red eyes burned."
    ^^^ flagged as "shadowed red eyes" (3 messages).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

from .text_segmentation import split_narration_sentences

DEBUG = "DEBUG_PHRASE_REPETITION" in os.environ

__all__ = [
    "detect_phrase_repetition",
    "PhraseResult",
    "FlaggedPhrase",
]


# ---------- public dataclasses ----------


@dataclass
class FlaggedPhrase:
    phrase: str
    count: int
    message_indices: list[int] = field(default_factory=list)
    example_sentences: list[str] = field(default_factory=list)


@dataclass
class PhraseResult:
    flagged_phrases: list[FlaggedPhrase]
    total_messages: int


# ---------- text processing ----------
# Paragraph/sentence/dialogue segmentation lives in text_segmentation so every
# audit pass splits text identically. `_split_sentences` strips dialogue.

_split_sentences = split_narration_sentences

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokenize(sentence: str) -> list[str]:
    return _TOKEN_RE.findall(sentence.lower())


# ---------- stopwords / content filter ----------

_STOPWORDS = frozenset(
    {
        # Articles
        "a",
        "an",
        "the",
        # Conjunctions
        "and",
        "or",
        "but",
        "nor",
        "yet",
        "so",
        # Prepositions
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "over",
        "under",
        "about",
        "across",
        "after",
        "before",
        # Be / aux
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "will",
        "would",
        "shall",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        # Pronouns / possessives
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "his",
        "her",
        "its",
        "their",
        "our",
        "your",
        "my",
        "mine",
        "him",
        "them",
        "us",
        "me",
        # Determiners / demonstratives
        "this",
        "that",
        "these",
        "those",
        # Wh-words
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        # Misc fillers
        "if",
        "then",
        "than",
        "while",
        "as",
        "not",
        "no",
        "just",
        "only",
        "even",
        "also",
        "very",
        "still",
        "now",
        "there",
        "here",
        # Common contractions (kept as single tokens by _TOKEN_RE)
        "i'm",
        "you're",
        "he's",
        "she's",
        "it's",
        "we're",
        "they're",
        "don't",
        "doesn't",
        "didn't",
        "won't",
        "wouldn't",
        "can't",
        "couldn't",
        "isn't",
        "aren't",
        "wasn't",
        "weren't",
        "hasn't",
        "haven't",
        "hadn't",
        "i've",
        "you've",
        "we've",
        "they've",
        "i'll",
        "you'll",
        "he'll",
        "she'll",
        "we'll",
        "they'll",
    }
)


def _count_content_words(gram: tuple[str, ...]) -> int:
    return sum(1 for w in gram if w not in _STOPWORDS)


# ---------- n-gram extraction & suppression ----------


def _ngrams(tokens: list[str], n: int):
    for i in range(len(tokens) - n + 1):
        yield tuple(tokens[i : i + n])


def _is_contiguous_sub(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    if len(short) >= len(long):
        return False
    for i in range(len(long) - len(short) + 1):
        if long[i : i + len(short)] == short:
            return True
    return False


def _suppress_subgrams(
    candidates: dict[tuple[str, ...], dict[int, str]],
) -> dict[tuple[str, ...], dict[int, str]]:
    """Drop n-grams that are contained in a longer n-gram with the same doc-set."""
    by_fingerprint: dict[frozenset[int], list[tuple[tuple[str, ...], dict[int, str]]]] = {}
    for gram, docs in candidates.items():
        fp = frozenset(docs.keys())
        by_fingerprint.setdefault(fp, []).append((gram, docs))

    survivors: dict[tuple[str, ...], dict[int, str]] = {}
    for items in by_fingerprint.values():
        # Longest first; keep only ones not contained in something already kept.
        items.sort(key=lambda x: len(x[0]), reverse=True)
        kept: list[tuple[str, ...]] = []
        for gram, docs in items:
            if any(_is_contiguous_sub(gram, k) for k in kept):
                continue
            kept.append(gram)
            survivors[gram] = docs
    return survivors


# ---------- public API ----------


def detect_phrase_repetition(
    messages: list[str],
    min_n: int = 3,
    max_n: int = 5,
    min_messages: int = 2,
    min_content_words: int = 2,
    require_last_message: bool = True,
) -> PhraseResult:
    """Detect distinctive phrases repeated across multiple messages.

    Args:
        messages: list of assistant messages. When `require_last_message` is
            True the final entry is treated as the current draft.
        min_n: minimum n-gram length (in tokens) to consider.
        max_n: maximum n-gram length (in tokens) to consider.
        min_messages: minimum number of *distinct* messages a phrase must
            appear in to be flagged.
        min_content_words: minimum non-stopword tokens in an n-gram. Filters
            out things like "in the air" / "I don't know" that are dense in
            grammatical glue.
        require_last_message: when True, only flag phrases that also appear
            in the last message — i.e. echoes in the current draft. Set to
            False to flag any repeated phrase across the whole list.

    Returns:
        PhraseResult sorted by (message_count desc, phrase length desc).
    """
    total = len(messages)
    if total < min_messages or min_n < 1 or max_n < min_n:
        return PhraseResult([], total)

    last_idx = total - 1

    # ngram -> {msg_idx: first example sentence found in that msg}
    ngram_docs: dict[tuple[str, ...], dict[int, str]] = {}

    for i, msg in enumerate(messages):
        seen_in_this_msg: set[tuple[str, ...]] = set()
        for sent in _split_sentences(msg):
            tokens = _tokenize(sent)
            if len(tokens) < min_n:
                continue
            for n in range(min_n, max_n + 1):
                if len(tokens) < n:
                    break
                for gram in _ngrams(tokens, n):
                    if gram in seen_in_this_msg:
                        continue
                    seen_in_this_msg.add(gram)
                    ngram_docs.setdefault(gram, {})[i] = sent

    if DEBUG:
        sys.stderr.write(f"[phrase_repetition] {len(ngram_docs)} unique n-grams\n")

    candidates: dict[tuple[str, ...], dict[int, str]] = {}
    for gram, docs in ngram_docs.items():
        if len(docs) < min_messages:
            continue
        if _count_content_words(gram) < min_content_words:
            continue
        if require_last_message and last_idx not in docs:
            continue
        candidates[gram] = docs

    if DEBUG:
        sys.stderr.write(f"[phrase_repetition] {len(candidates)} candidates after filters\n")

    survivors = _suppress_subgrams(candidates)

    if DEBUG:
        sys.stderr.write(f"[phrase_repetition] {len(survivors)} after sub-gram suppression\n")

    flagged: list[FlaggedPhrase] = []
    for gram, docs in survivors.items():
        ordered = sorted(docs.keys())
        flagged.append(
            FlaggedPhrase(
                phrase=" ".join(gram),
                count=len(docs),
                message_indices=ordered,
                example_sentences=[docs[i] for i in ordered],
            )
        )

    flagged.sort(key=lambda p: (-p.count, -len(p.phrase.split()), p.phrase))
    return PhraseResult(flagged_phrases=flagged, total_messages=total)
