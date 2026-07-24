"""
anti_echo.py — Detect when the assistant parrots the user's last message back as
an incredulous question.

Some models have a habit of bouncing the user's own words back as a question:

    H: "I have absolutely no money."
    A: "Absolutely no money?" She repeats.

    H: "I got some ice cream."
    A: He blinks, "Ice cream? You're a grown man."

Unlike every other detector in this package, this is a user→assistant check: it
compares the current draft against the user's immediately-preceding message.

Public API:
    detect_anti_echo(draft, user_message, *, max_question_words=10,
                     min_content_words=1, min_coverage=0.5, short_question_words=4)
    EchoResult, FlaggedEcho  (dataclasses)

How it works:
    - Only the user's spoken dialogue (text inside quote marks, with [OOC: ...]
      asides stripped) feeds the comparison pool. The user's narration and their
      out-of-character instructions are not in-character speech and shouldn't
      trigger a flag. Each quoted span is kept as its own token list so a match
      can't bridge two separate utterances.
    - Question candidates are gathered from the draft (sentences ending in ?),
      both from inside quote spans and from narration. Quotes are split out first
      so a lead-in like "He blinks, " can't fuse onto the quoted question.
    - For each candidate, find the longest contiguous word run it shares with any
      of the user's spoken spans. Flag it when that run carries enough content
      words (non-stopwords) and either the whole question is short or the shared
      run covers most of it — so bare-stopword questions ("You?", "What?") and
      questions that just happen to reuse one of the user's nouns don't fire.
"""

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


# ---------- public dataclasses ----------


@dataclass(slots=True)
class FlaggedEcho:
    echo: str  # the interrogative sentence flagged
    matched_phrase: str  # the contiguous run copied from the user (normalized)
    n_words: int  # length of that run, in words


@dataclass(slots=True)
class EchoResult:
    flagged_echoes: list[FlaggedEcho] = field(default_factory=list)


# ---------- text processing ----------
# Dialogue segmentation (strip_ooc, find_quote_spans, sentence splitting,
# ends_with_question) and token-sequence comparison (longest_common_run) live in
# the shared helper modules; the glue below assembles them into the user-dialogue
# pool and the draft's question candidates.


def _user_dialogue_runs(user_message: str) -> list[list[str]]:
    """The user's spoken dialogue, tokenized as one list per quoted span.

    [OOC: ...] asides are stripped first — their contents, including any inner
    quotes, are directives not speech. Each remaining quoted span becomes its
    own token list so a shared run can't bridge two separate utterances. A
    message with no quoted dialogue returns an empty list, leaving nothing to
    compare against."""
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
    """All question-mark-ending sentences from the draft, covering both quoted
    dialogue and narration. Quote spans are separated from their surrounding
    narration so a lead-in clause like "He blinks, " can't merge into the
    quoted question."""
    candidates: list[str] = []

    # Quoted dialogue: split each quote's inner text into its own sentences.
    for start, end in find_quote_spans(draft):
        inner = draft[start + 1 : end - 1]
        candidates.extend(split_sentences(inner))

    # Narration: dialogue is stripped, so quoted questions are not double-counted.
    candidates.extend(split_narration_sentences(draft))

    return [s for s in candidates if ends_with_question(s)]


# ---------- public entry point ----------


def detect_anti_echo(
    draft: str,
    user_message: str,
    *,
    max_question_words: int = 10,
    min_content_words: int = 1,
    min_coverage: float = 0.5,
    short_question_words: int = 4,
) -> EchoResult:
    """Flag questions in the draft that copy a contiguous run of the user's spoken dialogue.

    Only the user's spoken dialogue (text inside quote marks, with [OOC: ...]
    asides stripped) is used for comparison. The user's narration and
    out-of-character instructions are not in-character speech, so the assistant
    reusing words from them is not parroting. A user_message with no quoted
    dialogue produces no flags.

    Args:
        draft: The assistant's current response.
        user_message: The user's immediately-preceding message.
        max_question_words: Skip candidate questions longer than this — the
            parroting pattern is characteristically short.
        min_content_words: Minimum number of content words (non-stopwords) the
            copied run must carry to be worth flagging.
        min_coverage: For longer questions, the copied run must cover at least
            this fraction of the question's words. Prevents questions that merely
            reuse one of the user's nouns from firing.
        short_question_words: Questions at or below this word count bypass the
            coverage check — a short question that copies the user is an echo
            regardless of how much of it was copied.
    """
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
