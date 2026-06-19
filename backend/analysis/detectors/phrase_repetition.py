"""
phrase_repetition.py — Detect exact phrase repetition (n-gram level) across messages.

Catches stock phrases the model keeps reaching for across multiple turns, like
a description that reappears word-for-word in three different messages.

Public API:
    detect_phrase_repetition(messages, min_n=2, max_n=5, min_messages=2,
                             min_content_words=2, require_last_message=True)
    PhraseResult, FlaggedPhrase  (dataclasses)

How it works:
    - For each message: strip dialogue, split into sentences, tokenize.
    - Extract every n-gram (n in [min_n, max_n]) and track the set of distinct
      message indices where it appears. Repeated occurrences within one message
      don't inflate the count.
    - Drop n-grams with fewer than min_content_words non-stopword tokens — this
      filters out glue phrases like "in the air" or "I don't know".
    - Drop n-grams that appear in fewer than min_messages messages.
    - Drop sub-n-grams: if a shorter phrase is fully contained in a longer one
      that appears in exactly the same messages, the shorter one is redundant.
    - When require_last_message is True, only keep phrases that also appear in
      the final message (the current draft), making the report immediately
      actionable.

Example target pattern:
    Message 1: "His shadowed red eyes flickered in the firelight."
    Message 4: "She met the shadowed red eyes across the table."
    Message 7: "Behind the mask, his shadowed red eyes burned."
    ^^^ flagged as "shadowed red eyes" (3 messages).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from ..text.lexical import (
    count_content_words,
    is_contiguous_subsequence,
    ngrams,
    tokenize,
)
from ..text.text_segmentation import split_narration_sentences

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
# Segmentation lives in text_segmentation so every detector splits text the
# same way. _split_sentences strips dialogue before splitting into sentences.

_split_sentences = split_narration_sentences


# ---------- n-gram suppression ----------
# n-gram extraction and the contiguous-containment test both live in lexical
# (shared, were duplicated here); this module keeps only the suppression policy.


def _suppress_subgrams(
    candidates: dict[tuple[str, ...], dict[int, str]],
) -> dict[tuple[str, ...], dict[int, str]]:
    """Drop shorter n-grams that are fully contained in a longer n-gram appearing in the same set of messages."""
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
            if any(is_contiguous_subsequence(gram, k) for k in kept):
                continue
            kept.append(gram)
            survivors[gram] = docs
    return survivors


# ---------- public API ----------


def detect_phrase_repetition(
    messages: list[str],
    min_n: int = 2,
    max_n: int = 5,
    min_messages: int = 2,
    min_content_words: int = 2,
    require_last_message: bool = True,
) -> PhraseResult:
    """Detect phrases that recur word-for-word across multiple messages.

    Args:
        messages: list of assistant messages. When require_last_message is True,
            the final entry is treated as the current draft.
        min_n: minimum phrase length in words.
        max_n: maximum phrase length in words.
        min_messages: how many distinct messages a phrase must appear in to be flagged.
        min_content_words: minimum content words (non-stopwords) in a phrase.
            Filters out grammatical glue like "in the air" or "I don't know".
            Load-bearing at min_n=2: a value of 2 forces both tokens of a 2-gram
            to be content words, so 2-word flags can't degenerate into a
            single-word match dressed up with a stopword ("the eyes", "his gaze").
        require_last_message: when True, only flag phrases that also appear in
            the last message. Set to False to flag any repeated phrase across the
            full list.

    Returns:
        PhraseResult sorted by (message count descending, phrase length descending).
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
            tokens = tokenize(sent)
            if len(tokens) < min_n:
                continue
            for n in range(min_n, max_n + 1):
                if len(tokens) < n:
                    break
                for gram in ngrams(tokens, n):
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
        if count_content_words(gram) < min_content_words:
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
