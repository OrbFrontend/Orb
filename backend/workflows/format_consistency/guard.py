"""Validate that a voice rewrite can replace the finished draft."""

from __future__ import annotations

import re

from ..toolkit import protected_runs, spoken_lines

# A restatement may be slightly shorter, but must not grow past the Editor's cap.
_MIN_WORD_RATIO = 0.75
_MAX_EXTRA_WORDS = 2

# Match a leading speaker label only when the rewrite introduces one.
_SPEAKER_LABEL = re.compile(
    r"\A[ \t]*(?:\#{1,6}[ \t]+)?(?:\*\*|__|\[)?[ \t]*[^\s:][^\n:]{0,39}(?:\*\*|__|\])?[ \t]*:",
)

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _paragraphs(text: str) -> int:
    return len([p for p in _PARAGRAPH_BREAK.split(text) if p.strip()])


def rejection(draft: str, rewritten: str) -> str:
    """Return why *rewritten* is rejected, or ``""`` when it is acceptable."""
    if not rewritten.strip():
        return "empty"

    draft_words = len(draft.split())
    words = len(rewritten.split())
    if words > draft_words + _MAX_EXTRA_WORDS:
        return f"grew from {draft_words} to {words} words"
    if words < draft_words * _MIN_WORD_RATIO:
        return f"shrank from {draft_words} to {words} words"

    draft_paras, paras = _paragraphs(draft), _paragraphs(rewritten)
    if paras != draft_paras:
        return f"paragraph count {draft_paras} -> {paras}"

    if sorted(protected_runs(draft)) != sorted(protected_runs(rewritten)):
        return "protected markup changed"

    # Compare dialogue content, not markers; markup normalization runs afterward.
    haystack = " ".join(rewritten.split())
    missing = [line for line in spoken_lines(draft) if line not in haystack]
    if missing:
        return f"dropped or reworded {len(missing)} of {len(spoken_lines(draft))} spoken lines"

    if _SPEAKER_LABEL.match(rewritten) and not _SPEAKER_LABEL.match(draft):
        return "added a speaker label"

    return ""
