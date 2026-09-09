"""Decide whether a voice rewrite may replace the finished draft.

The forced call lands late: after the Editor's audit and length passes, and
after the pipeline stripped the model's self-label. Whatever it returns *is* the
reply, so an imperfect answer does not degrade the voice fix -- it undoes a pass
that already ran, or changes the story. The tool contract says a restatement
changes the narrator and nothing else; this is that contract read back off the
result, and a rewrite that fails any part of it is discarded in favour of the
draft. The drift stays; the reply does not change. That trade is always the
right way round, because the draft is a finished reply and the rewrite is a
guess about it.
"""

from __future__ import annotations

import re

from ..toolkit import protected_runs, spoken_lines

#: A restatement may come back a little shorter (a tense change can drop an
#: auxiliary), never longer. The Editor's length guard is a ceiling this pass is
#: not allowed to lift, and the tool is told in as many words to add nothing.
_MIN_WORD_RATIO = 0.75
_MAX_EXTRA_WORDS = 2

#: A leading ``Name:`` in any of the forms the writer pass strips. Matched only
#: to reject a rewrite that *introduced* one, so a false positive costs a
#: discarded rewrite rather than a mangled reply.
_SPEAKER_LABEL = re.compile(
    r"\A[ \t]*(?:\#{1,6}[ \t]+)?(?:\*\*|__|\[)?[ \t]*[^\s:][^\n:]{0,39}(?:\*\*|__|\])?[ \t]*:",
)

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _paragraphs(text: str) -> int:
    return len([p for p in _PARAGRAPH_BREAK.split(text) if p.strip()])


def rejection(draft: str, rewritten: str) -> str:
    """Why *rewritten* may not stand in for *draft*, or ``""`` when it may.

    Every check is a comparison against the draft rather than an absolute rule,
    so a passage that already had four paragraphs, a code fence or a leading
    label keeps them without argument.
    """
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

    # Content, not markers: the markup pass runs after this and is expected to
    # put the quotes back where the convention wants them. What it cannot repair
    # is a line nobody says any more, so each spoken line has to still be in
    # there somewhere -- quoted, bare, or moved.
    haystack = " ".join(rewritten.split())
    missing = [line for line in spoken_lines(draft) if line not in haystack]
    if missing:
        return f"dropped or reworded {len(missing)} of {len(spoken_lines(draft))} spoken lines"

    if _SPEAKER_LABEL.match(rewritten) and not _SPEAKER_LABEL.match(draft):
        return "added a speaker label"

    return ""
