"""Detect exact phrase repetition across messages."""

from __future__ import annotations

import os
import sys
import unicodedata
from dataclasses import dataclass, field

from ..text.lexical import STOPWORDS, TOKEN_RE, count_content_words, is_contiguous_subsequence, ngrams, tokenize
from ..text.roleplay_segmentation import split_narration_sentences

DEBUG = "DEBUG_PHRASE_REPETITION" in os.environ

__all__ = ["detect_phrase_repetition", "detect_phrase_reuse", "deduplicate_phrases", "PhraseResult", "FlaggedPhrase"]


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
                phrase=" ".join(gram), count=len(docs), message_indices=ordered, example_sentences=[docs[i] for i in ordered]
            )
        )

    flagged = deduplicate_phrases(flagged)

    if DEBUG:
        sys.stderr.write(f"[phrase_repetition] {len(flagged)} after redundancy suppression\n")

    return PhraseResult(flagged_phrases=flagged, total_messages=total)


def _is_proper_noun(phrase: str, sentence: str) -> bool:
    """Return whether every content word of *phrase* is capitalised where it occurs in *sentence*."""
    gram = phrase.split()
    originals = TOKEN_RE.findall(unicodedata.normalize("NFC", sentence))
    folded = tokenize(sentence)
    if len(originals) != len(folded):
        return False
    for i in range(len(folded) - len(gram) + 1):
        if folded[i : i + len(gram)] == gram:
            content = [word for word, token in zip(originals[i:], gram) if token not in STOPWORDS]
            return bool(content) and all(word[:1].isupper() for word in content)
    return False


def detect_phrase_reuse(
    earlier: str,
    text: str,
    min_n: int = 2,
    short_max_n: int = 2,
    max_n: int = 5,
    short_min_sentences: int = 3,
    long_min_sentences: int = 2,
    min_content_words: int = 2,
) -> PhraseResult:
    """Return phrases a sentence of *text* repeats from an earlier sentence of *earlier* or of *text* itself.

    One finding per repeating sentence, with ``example_sentences`` holding the previous occurrence and the repeat. Names are
    not findings.
    """
    before = len(_split_sentences(earlier))
    sentences = _split_sentences(earlier) + _split_sentences(text)

    def repeats(phrase: FlaggedPhrase, need: int) -> list[int]:
        return [k for k, index in enumerate(phrase.message_indices) if index >= before and k >= need - 1]

    need_of: dict[int, int] = {}
    candidates: list[FlaggedPhrase] = []
    for low, high, need in ((min_n, short_max_n, short_min_sentences), (short_max_n + 1, max_n, long_min_sentences)):
        found = detect_phrase_repetition(
            sentences,
            min_n=low,
            max_n=high,
            min_messages=need,
            min_content_words=min_content_words,
            require_last_message=False,
        )
        for phrase in found.flagged_phrases:
            if repeats(phrase, need):
                need_of[id(phrase)] = need
                candidates.append(phrase)

    flagged: list[FlaggedPhrase] = []
    for phrase in deduplicate_phrases(candidates):
        for k in repeats(phrase, need_of[id(phrase)]):
            sentence = phrase.example_sentences[k]
            if _is_proper_noun(phrase.phrase, sentence):
                continue
            flagged.append(
                FlaggedPhrase(
                    phrase=phrase.phrase,
                    count=phrase.count,
                    message_indices=phrase.message_indices[: k + 1],
                    example_sentences=[phrase.example_sentences[k - 1], sentence],
                )
            )
    return PhraseResult(flagged_phrases=flagged, total_messages=len(sentences))
