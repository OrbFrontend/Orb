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
    "deduplicate_phrases",
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


# ---------- redundancy suppression ----------
# n-gram extraction and the contiguous-containment test both live in lexical
# (shared, were duplicated here); this module keeps only the suppression policy.
# deduplicate_phrases is the single source of truth, used here and by audit's
# two-pass merge so the same phrase can't surface twice across passes.


def _rank(p: FlaggedPhrase) -> tuple[int, int, str]:
    """Best-first ordering: most frequent, then longest, then alphabetical."""
    return (-p.count, -len(p.phrase.split()), p.phrase)


def _overlap_chains(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True when a and b tile a longer phrase head-to-tail — a suffix of one
    equals a prefix of the other — and the shared run carries a content word.
    Catches one long repeat surfacing as two overlapping fragments, e.g.
    "the tight ring" + "ring of muscle" (overlap "ring"). The content-word
    requirement stops stopword joints ("into the" + "the dark") from merging."""
    for x, y in ((a, b), (b, a)):
        for k in range(min(len(x), len(y)) - 1, 0, -1):  # k < len excludes containment
            if x[-k:] == y[:k] and count_content_words(x[-k:]):
                return True
    return False


def deduplicate_phrases(phrases: list[FlaggedPhrase]) -> list[FlaggedPhrase]:
    """Drop phrases that restate the same underlying repeat as another.

    A shorter phrase contiguously contained in a longer one always recurs in a
    superset of the longer's messages, so for each containment pair keep:
      - the longer phrase when both span the same messages (more specific), else
      - the shorter phrase, which recurs more widely (the longer is a partial
        coincidence: "six centuries" in 3 msgs subsumes "for six centuries" in 2).
    For phrases that merely overlap head-to-tail across the same messages, keep
    the higher-ranked one ("ring of muscle" / "the tight ring" -> one line).
    Survivors are returned best-first (_rank order); callers need not re-sort.
    """
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
