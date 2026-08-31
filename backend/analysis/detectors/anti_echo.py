"""Detect questions that echo the user's quoted dialogue."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..text.lexical import count_content_words, longest_common_run, tokenize
from ..text.text_segmentation import (
    ends_with_question,
    find_quote_spans,
    split_narration_sentences,
    split_sentences,
    strip_ooc,
)

__all__ = [
    "detect_anti_echo",
    "EchoResult",
    "FlaggedEcho",
]


@dataclass(slots=True)
class FlaggedEcho:
    echo: str  # the interrogative sentence flagged
    matched_phrase: str  # the contiguous run copied from the user (normalized)
    n_words: int  # length of that run, in words


@dataclass(slots=True)
class EchoResult:
    flagged_echoes: list[FlaggedEcho] = field(default_factory=list)


def _user_dialogue_runs(user_message: str) -> list[list[str]]:
    """Return the user's quoted dialogue as token runs."""
    cleaned = strip_ooc(user_message)
    runs: list[list[str]] = []
    for start, end in find_quote_spans(cleaned):
        toks = tokenize(cleaned[start + 1 : end - 1])
        if toks:
            runs.append(toks)
    return runs


def _longest_run_against_any(candidate: list[str], user_runs: list[list[str]]) -> list[str]:
    """Longest contiguous word sequence candidate shares with any single user spoken span."""
    best: list[str] = []
    for toks in user_runs:
        run = longest_common_run(candidate, toks)
        if len(run) > len(best):
            best = run
    return best


def _interrogative_candidates(draft: str) -> list[str]:
    """Return question-mark-ending sentences from dialogue and narration."""
    candidates: list[str] = []

    # Quoted dialogue: split each quote's inner text into its own sentences.
    for start, end in find_quote_spans(draft):
        inner = draft[start + 1 : end - 1]
        candidates.extend(split_sentences(inner))

    # Narration: dialogue is stripped, so quoted questions are not double-counted.
    candidates.extend(split_narration_sentences(draft))

    return [s for s in candidates if ends_with_question(s)]


def detect_anti_echo(
    draft: str,
    user_message: str,
    *,
    max_question_words: int = 10,
    min_content_words: int = 1,
    min_coverage: float = 0.5,
    short_question_words: int = 4,
) -> EchoResult:
    """Flag draft questions that copy a run of the user's quoted dialogue."""
    if not draft or not user_message:
        return EchoResult()

    user_runs = _user_dialogue_runs(user_message)
    if not user_runs:
        return EchoResult()

    flagged: list[FlaggedEcho] = []
    seen: set[str] = set()
    for sentence in _interrogative_candidates(draft):
        key = sentence.strip()
        if key in seen:
            continue
        seen.add(key)

        c_tokens = tokenize(sentence)
        if not c_tokens or len(c_tokens) > max_question_words:
            continue

        run = _longest_run_against_any(c_tokens, user_runs)
        if not run:
            continue
        if count_content_words(run) < min_content_words:
            continue
        if len(c_tokens) > short_question_words and len(run) / len(c_tokens) < min_coverage:
            continue

        flagged.append(FlaggedEcho(echo=key, matched_phrase=" ".join(run), n_words=len(run)))

    return EchoResult(flagged_echoes=flagged)
