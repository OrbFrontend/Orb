"""Trim exact context copies from editor replacements and repair the seams deletions leave."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..core.text_segmentation import HARD_LINE_BREAK_RE, PARA_SPLIT
from .audit import OUTER_MARKERS
from .text.roleplay_segmentation import extract_block_spans

__all__ = ["HealedPatch", "heal_deletion", "heal_replacement"]


@dataclass(frozen=True)
class HealedPatch:
    """A replacement ready to splice, or a rejection reason."""

    start: int
    end: int
    replace: str
    notes: tuple[str, ...] = ()
    rejection: str | None = None


_WORD_RE = re.compile(r"\S+")


def _word_spans(text: str) -> list[tuple[int, int]]:
    """Return whitespace-delimited word offsets."""
    return [match.span() for match in _WORD_RE.finditer(text)]


def _key(word: str) -> str:
    """Return the case-folded comparison key for a word."""
    return word.casefold()


def _neighbour_keys(text: str) -> list[str]:
    """Return comparison keys for non-punctuation neighbour words."""
    return [_key(word) for word in _WORD_RE.findall(text) if any(ch.isalnum() for ch in word)]


def _tail_repeat(keys: Sequence[str], following: Sequence[str]) -> int:
    """Return the trailing replacement words repeated after the span."""
    for k in range(min(len(keys), len(following)), 0, -1):
        if list(keys[-k:]) == list(following[:k]):
            return k
    return 0


def _head_repeat(keys: Sequence[str], preceding: Sequence[str]) -> int:
    """How many leading words of the replacement repeat the draft behind it."""
    for k in range(min(len(keys), len(preceding)), 0, -1):
        if list(keys[:k]) == list(preceding[-k:]):
            return k
    return 0


# Target spans are marker-stripped (``audit.strip_markers``), so the quotes or emphasis around flagged text stay in the draft on
# either side of the span. Straight and curly double quotes are one mark; a curly apostrophe stays apart from them so ``sayin\u2019``
# never reads as a closing quote.
_MARKER_KIND = str.maketrans({"“": '"', "”": '"', "’": "‘"})


def _leading_markers(text: str) -> str:
    """Return the quote/emphasis markers that open *text*."""
    return text[: len(text) - len(text.lstrip(OUTER_MARKERS))]


def _trailing_markers(text: str) -> str:
    """Return the quote/emphasis markers that close *text*."""
    return text[len(text.rstrip(OUTER_MARKERS)) :]


def _enclosing_block(draft: str, start: int, end: int) -> tuple[int, int] | None:
    """Return the speech or emphasis block holding ``draft[start:end]``, if any."""
    para_start = 0
    for match in PARA_SPLIT.finditer(draft, 0, start):
        para_start = match.end()
    next_break = PARA_SPLIT.search(draft, end)
    para_end = next_break.start() if next_break else len(draft)
    for typ, block_start, block_end in extract_block_spans(draft[para_start:para_end]):
        if typ != "NARRATION" and para_start + block_start < start and end < para_start + block_end:
            return para_start + block_start, para_start + block_end
    return None


def _wrapping_markers(draft: str, start: int, end: int) -> tuple[str, str]:
    """Return the markers the draft already has before and after the span.

    Each side is the run touching the span, plus the enclosing block's own
    delimiter when the span sits mid-block and that delimiter is further off.
    """
    before = _trailing_markers(draft[:start])
    after = _leading_markers(draft[end:])
    block = _enclosing_block(draft, start, end)
    if block is not None:
        block_start, block_end = block
        if start - len(before) > block_start:
            before = _leading_markers(draft[block_start:start]) + before
        if end + len(after) < block_end:
            after += _trailing_markers(draft[end:block_end])
    return before, after


def _trim_wrapping_markers(draft: str, start: int, end: int, text: str) -> tuple[str, list[str]]:
    """Drop the outer markers of *text* that the draft already wraps around the span."""
    before, after = _wrapping_markers(draft, start, end)
    notes: list[str] = []
    opening = _head_repeat(_leading_markers(text).translate(_MARKER_KIND), before.translate(_MARKER_KIND))
    if opening:
        text = text[opening:].lstrip()
        notes.append(f"dropped {opening} opening marker(s) the draft already has before the span")
    closing = _tail_repeat(_trailing_markers(text).translate(_MARKER_KIND), after.translate(_MARKER_KIND))
    if closing:
        text = text[:-closing].rstrip()
        notes.append(f"dropped {closing} closing marker(s) the draft already has after the span")
    return text, notes


_SEPARATORS = ("", " ", "\n", "\n\n")


def _separator_strength(run: str) -> int:
    """Index into :data:`_SEPARATORS` for the break a whitespace run represents."""
    run = run.replace("\r\n", "\n")
    if PARA_SPLIT.search(run):
        return 3
    if HARD_LINE_BREAK_RE.search(run):
        return 2
    return 1 if run else 0


_PAIRS = {"*": "*", "_": "_", '"': '"', "“": "”", "(": ")"}
_CLOSERS = frozenset(_PAIRS.values())
_TERMINAL = ".!?…—–"
_ORPHAN_RE = re.compile(r"[.,;:!?…]+(?=\s|$|[*_\"”])")
_PRIOR_MARK_RE = re.compile(r"([.,;:!?…—–])([\"”’)*_]*)$")
_ENDS_SENTENCE_RE = re.compile(r"(?<!\.\.)[.!?][\"”’)*_]*$")
_REMOVED_TERMINAL_RE = re.compile(r"([.!?…]+)[\"”’)*_]*$")
_SENTENCE_START_RE = re.compile(r"(?:^|[.!?…][\"”’)*_]*\s+|\n)[\"“*_(\s]*([^.!?…\n]*)$")
_QUESTION_OPENER_RE = re.compile(
    r"(?:wh\w+|how|do|does|did|is|are|was|were|am|can|could|will|would|shall|should|have|has|had|may|might|must)(?:n't)?\b",
    re.IGNORECASE,
)
_WS_RUN_RE = re.compile(r"\s+")


def _opens(text: str) -> bool:
    """Whether *text* ends in a quote or emphasis opener."""
    if not text or text[-1] not in _PAIRS:
        return False
    if text[-1] in "“(" or len(text) == 1 or text[-2].isspace() or text[-2] in "([":
        return True
    return (text[-2] in "—–" and text[-1] in "*_") or _opens(text[:-1])


def _closes(text: str) -> bool:
    """Whether *text* starts with a quote or emphasis closer."""
    if not text or text[0] not in _CLOSERS:
        return False
    if text[0] in "”)" or len(text) == 1:
        return True
    if text[1] in _CLOSERS:
        return _closes(text[1:])
    if text.startswith(("...", "…"), 1):
        return False
    return text[1].isspace() or text[1] in ".,;:!?)]’'" or (text[1] in "—–" and text[0] in "*_")


def _odd_count(line: str, mark: str) -> bool:
    """Whether *line* holds an odd number of a symmetric *mark*, so one is still waiting for its partner."""
    return _PAIRS.get(mark) == mark and line.count(mark) % 2 == 1


def _stranded_markers(text: str, start: int, end: int) -> tuple[str, str]:
    """The markers ``text[start:end]`` takes from pairs it does not hold whole: (closers for the left, openers for the right)."""
    closers = ""
    stack: list[str] = []
    for index in range(start, end):
        mark = text[index]
        if mark not in _PAIRS and mark not in _CLOSERS:
            continue
        closes = _closes(text[index:])
        if closes and stack and _PAIRS[stack[-1]] == mark:
            stack.pop()
        elif closes:
            closers += mark
        elif mark in _PAIRS and _opens(text[: index + 1]):
            stack.append(mark)
    return closers, "".join(stack)


def _carried_terminal(removed: str, left: str) -> str:
    """The sentence end a deletion took with it when it cut a sentence's last clause, else ``""``."""
    body = removed.strip()
    match = _REMOVED_TERMINAL_RE.search(body)
    if not match or not left[-1:].isalnum() or len(body.split()) < 2:
        return ""
    mark = match.group(1)
    if "?" in mark and body.startswith(","):
        sentence = _SENTENCE_START_RE.search(left)
        if not (sentence and _QUESTION_OPENER_RE.match(sentence.group(1))):
            return "."
    return mark


def _capitalize_first_letter(text: str) -> str:
    """Uppercase the first letter of *text*, past any opening markers, when it is lowercase."""
    body = text.lstrip("".join(_PAIRS))
    if body[:1].islower():
        index = len(text) - len(body)
        return text[:index] + body[0].upper() + body[1:]
    return text


def heal_deletion(draft: str, start: int, end: int) -> tuple[int, int, str]:
    """Splice out ``draft[start:end]`` and repair the seam it leaves; return ``(start, end, replacement)`` to splice instead.

    A deletion can strand what belonged to the text around it: whitespace on both sides, an emptied ``**`` or ``""`` pair, one
    marker of a pair, punctuation with nothing left to end, a sentence's last clause together with its full stop, or a
    lowercase word that now starts the sentence.
    """
    removed = draft[start:end]
    original_left = draft[:start]
    original_right = draft[end:]
    left = original_left.rstrip()
    right = original_right.lstrip()

    gap = draft[len(left) : len(draft) - len(right)]
    gap_strength = max((_separator_strength(run) for run in _WS_RUN_RE.findall(gap)), default=0)

    sentence_start = not left or gap_strength >= 2 or _opens(left) or bool(_ENDS_SENTENCE_RE.search(left))
    left += _carried_terminal(removed, left)
    closers, openers = _stranded_markers(draft, start, end)
    left_line, right_line = left[left.rfind("\n") + 1 :], right.split("\n", 1)[0]
    waiting_openers = _stranded_markers(left_line, 0, len(left_line))[1]
    waiting_closers = _stranded_markers(right_line, 0, len(right_line))[0]
    left += "".join(
        mark for mark in closers if _odd_count(left_line, mark) or any(_PAIRS[opener] == mark for opener in waiting_openers)
    )
    openers = "".join(mark for mark in openers if _odd_count(right_line, mark) or _PAIRS[mark] in waiting_closers)

    while True:
        if _opens(left) and right[:1] == _PAIRS[left[-1]] and _closes(right):
            left, right = left[:-1].rstrip(), right[1:].lstrip()
            continue
        if len(left) > 1 and _opens(left[:-1]) and left[-1] == _PAIRS[left[-2]]:
            left = left[:-2].rstrip()
            continue
        if openers and right[:1] == _PAIRS[openers[-1]] and _closes(right):
            openers, right = openers[:-1], right[1:].lstrip()
            continue
        orphan = _ORPHAN_RE.match(right)
        if orphan:
            block_start = bool(openers) or not left or gap_strength >= 2 or _opens(left)
            prior = None if block_start else _PRIOR_MARK_RE.search(left)
            if prior and prior.group(1) not in _TERMINAL and orphan.group(0)[0] in _TERMINAL:
                left = left[: prior.start()] + orphan.group(0) + prior.group(2)
            if prior or block_start:
                right = right[orphan.end() :].lstrip()
                continue
        break
    right = openers + right

    body = removed.strip()
    continues = original_right.lstrip().lstrip("".join(_PAIRS))[:1].islower()
    if sentence_start and continues and body.lstrip("".join(_PAIRS))[:1].isupper() and not body.endswith(tuple(_TERMINAL)):
        right = _capitalize_first_letter(right)

    kept_left = len(os.path.commonprefix([original_left, left]))
    kept_right = len(os.path.commonprefix([original_right[::-1], right[::-1]]))
    replaced = draft[kept_left : len(draft) - kept_right]
    strength = max((_separator_strength(run) for run in _WS_RUN_RE.findall(replaced)), default=0)
    if not left or not right:
        separator = ""
    elif strength < 2 and (_opens(left) or _closes(right) or _ORPHAN_RE.match(right)):
        separator = ""
    else:
        separator = _SEPARATORS[strength]
    return kept_left, len(draft) - kept_right, left[kept_left:] + separator + right[: len(right) - kept_right]


def heal_replacement(draft: str, start: int, end: int, replace: str, *, restatement_deletes: bool = False) -> HealedPatch:
    """Trim repeated context from one replacement.

    A replacement that heals away entirely is rejected as a mis-aim, unless *restatement_deletes*: for a finding whose fix is
    removal, restating the neighbours is how a model says "drop this", so it splices as a deletion.
    """
    text = replace.strip()
    spans = _word_spans(text)
    keys = [_key(text[s:e]) for s, e in spans]
    notes: list[str] = []
    lo, hi = 0, len(spans)

    trailing = _tail_repeat(keys, _neighbour_keys(draft[end:]))
    if trailing:
        hi -= trailing
        notes.append(f"trimmed {trailing} trailing word(s) copied from the draft after the span")
    leading = _head_repeat(keys[lo:hi], _neighbour_keys(draft[:start]))
    if leading:
        lo += leading
        notes.append(f"trimmed {leading} leading word(s) copied from the draft before the span")
    if lo or hi < len(spans):
        text = text[spans[lo][0] : spans[hi - 1][1]].strip() if lo < hi else ""
    text, marker_notes = _trim_wrapping_markers(draft, start, end, text)
    notes.extend(marker_notes)

    if text:
        return HealedPatch(start, end, text, tuple(notes))

    if replace.strip():
        if not restatement_deletes:
            return HealedPatch(
                start,
                end,
                replace,
                tuple(notes),
                rejection="only repeats text that already surrounds the flagged span — send new prose for the flagged text itself",
            )
        notes.append("nothing new remained, so the span is deleted")

    new_start, new_end, seam = heal_deletion(draft, start, end)
    if (new_start, new_end, seam) != (start, end, ""):
        notes.append("healed the seam the deletion left")
    return HealedPatch(new_start, new_end, seam, tuple(notes))
