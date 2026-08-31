"""Detect exact phrase repetition across messages."""

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
    "deduplicate_phrases",
    "PhraseResult",
    "FlaggedPhrase",
]


@dataclass(slots=True)
class FlaggedPhrase:
    phrase: str
    count: int
    message_indices: list[int] = field(default_factory=list)
    example_sentences: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PhraseResult:
    flagged_phrases: list[FlaggedPhrase]
    total_messages: int


_split_sentences = split_narration_sentences


def _rank(p: FlaggedPhrase) -> tuple[int, int, str]:
    """Best-first ordering: most frequent, then longest, then alphabetical."""
    return (-p.count, -len(p.phrase.split()), p.phrase)


def _overlap_chains(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Return whether two phrases overlap on a meaningful token run."""
    for x, y in ((a, b), (b, a)):
        for k in range(min(len(x), len(y)) - 1, 0, -1):  # k < len excludes containment
            if x[-k:] == y[:k] and count_content_words(x[-k:]):
                return True
    return False


def deduplicate_phrases(phrases: list[FlaggedPhrase]) -> list[FlaggedPhrase]:
    """Drop phrases that describe the same underlying repeat."""
    grams = {id(p): tuple(p.phrase.split()) for p in phrases}
    msgs = {id(p): frozenset(p.message_indices) for p in phrases}
    suppressed: set[int] = set()
    for i, a in enumerate(phrases):
        for b in phrases[i + 1 :]:
            ga, gb = grams[id(a)], grams[id(b)]
            short, long = (a, b) if len(ga) <= len(gb) else (b, a)
            sg, lg = grams[id(short)], grams[id(long)]
            if len(sg) < len(lg) and is_contiguous_subsequence(sg, lg):
                same = msgs[id(short)] == msgs[id(long)]
                suppressed.add(id(short) if same else id(long))
            elif msgs[id(a)] == msgs[id(b)] and _overlap_chains(ga, gb):
                suppressed.add(id(max((a, b), key=_rank)))
    return sorted((p for p in phrases if id(p) not in suppressed), key=_rank)


def detect_phrase_repetition(
    messages: list[str],
    min_n: int = 2,
    max_n: int = 5,
    min_messages: int = 2,
    min_content_words: int = 2,
    require_last_message: bool = True,
) -> PhraseResult:
    """Return repeated phrases across the supplied messages."""
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

    flagged: list[FlaggedPhrase] = []
    for gram, docs in candidates.items():
        ordered = sorted(docs.keys())
        flagged.append(
            FlaggedPhrase(
                phrase=" ".join(gram),
                count=len(docs),
                message_indices=ordered,
                example_sentences=[docs[i] for i in ordered],
            )
        )

    flagged = deduplicate_phrases(flagged)

    if DEBUG:
        sys.stderr.write(f"[phrase_repetition] {len(flagged)} after redundancy suppression\n")

    return PhraseResult(flagged_phrases=flagged, total_messages=total)
