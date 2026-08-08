"""healing.py — Repair for editor patches that restate the draft around them.

:func:`analysis.patching.apply_id_patches` splices the model's ``replace`` text
over the exact offsets the audit recorded, so a patch is only as good as the
model's aim. Two mis-aims recur across providers, and both turn into visible
duplication the moment the splice lands:

* **Restating the sentence before.** ``"I'm bored." She murmured.`` with the
  dialogue tag flagged comes back as ``"I'm bored."`` — the model meant to drop
  the tag, and expressed it by rewriting the line it was *not* asked to touch.
  Spliced verbatim the draft reads ``"I'm bored." "I'm bored."``.
* **Swallowing the sentence after.** ``The air smelled like ozone. The only
  sound was the wind.`` with the first sentence flagged comes back as ``The air
  was crisp. The only sound was the wind.`` — the tail copies text that is still
  sitting in the draft, so the splice prints it twice.

Both are one defect: the replacement carries sentences that duplicate draft text
*outside* the target span. That text was never in scope, it is already in the
draft, and a copy of it is therefore never new prose — so it can always be
dropped. Healing trims those sentences off either end of the replacement and
closes the whitespace a trim (or a deliberate deletion) strands.

**The comparison is whole-sentence and exact after normalisation — never fuzzy.**
Genuinely similar neighbours score high on any similarity ratio ("He nodded." /
"She nodded." is 0.95 by ``difflib``), and trimming one of those would delete
prose the model wrote on purpose. Exact-after-normalisation cannot make that
mistake: the only thing it can remove is text that already exists, unchanged,
next door.

**Healing never deletes on the model's behalf.** A replacement that is *entirely*
restated context contributes no new prose at all, so the patch is rejected as a
no-op and the caller feeds the reason back — the same contract as a ``replace``
that repeats the flagged text unchanged. Splicing the empty string instead would
delete the flagged sentence on a guess at intent, and ``targets.py`` treats
silent data loss as the hazard this whole path is designed against.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .text.text_segmentation import HARD_LINE_BREAK_RE, PARA_SPLIT, split_sentence_units

__all__ = ["HealedPatch", "heal_replacement"]


@dataclass(frozen=True)
class HealedPatch:
    """One patch ready to splice into ``[start:end]`` — or a reason it must not be.

    ``start``/``end`` can widen past the target's own offsets: a deletion
    absorbs the separator whitespace that would otherwise be left doubled.
    """

    start: int
    end: int
    replace: str
    notes: tuple[str, ...] = ()
    rejection: str | None = None


# ── Sentence comparison ───────────────────────────────────────────────────────


def _key(sentence: str) -> str:
    """Comparison key for "this is the same sentence".

    Case and whitespace only. Punctuation and emphasis markers stay significant:
    ``The wind howled.`` and ``The wind howled!`` are different sentences, and
    the whole guard against over-trimming is that only an unchanged copy matches.
    """
    return " ".join(sentence.split()).casefold()


def _neighbour_keys(text: str) -> list[str]:
    """Comparison keys for the draft on one side of the span, nearest last/first.

    Punctuation-only units are dropped. A target span excludes the emphasis
    markers wrapping it (``audit._strip_markers``), so the draft left behind can
    end in a dangling ``*`` — as its own unit that fragment would sit between the
    replacement and the sentence it actually duplicates, and hide the repeat.
    """
    return [_key(unit) for unit in split_sentence_units(text) if any(ch.isalnum() for ch in unit)]


def _unit_spans(text: str) -> list[tuple[int, int]]:
    """Offsets of each sentence unit within *text*.

    Trimming works on offsets rather than on the unit strings because
    ``split_sentence_units`` drops the separators: rejoining its output would
    flatten a replacement that spans two paragraphs onto one line. Units are
    source-contiguous and in order (splitting only strips their edges), so a
    forward scan locates each one exactly.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for unit in split_sentence_units(text):
        idx = text.find(unit, cursor)
        if idx < 0:  # unreachable while units stay source-contiguous; degrade to "no trim"
            return []
        spans.append((idx, idx + len(unit)))
        cursor = idx + len(unit)
    return spans


def _tail_repeat(keys: Sequence[str], following: Sequence[str]) -> int:
    """How many trailing sentences of the replacement repeat the draft ahead of it.

    Longest overlap first: a replacement ending ``[A, B]`` in front of a draft
    starting ``[A, B, C]`` repeats *two* sentences, and testing k=1 first would
    compare ``B`` against ``A``, miss, and leave both duplicated.
    """
    for k in range(min(len(keys), len(following)), 0, -1):
        if list(keys[-k:]) == list(following[:k]):
            return k
    return 0


def _head_repeat(keys: Sequence[str], preceding: Sequence[str]) -> int:
    """How many leading sentences of the replacement repeat the draft behind it."""
    for k in range(min(len(keys), len(preceding)), 0, -1):
        if list(keys[:k]) == list(preceding[-k:]):
            return k
    return 0


# ── Deletion seam ─────────────────────────────────────────────────────────────


_SEPARATORS = ("", " ", "\n", "\n\n")


def _separator_strength(run: str) -> int:
    """Index into :data:`_SEPARATORS` for the break a whitespace run represents."""
    if PARA_SPLIT.search(run):
        return 3
    if HARD_LINE_BREAK_RE.search(run):
        return 2
    return 1 if run else 0


def _collapse_deletion_seam(draft: str, start: int, end: int) -> tuple[int, int, str]:
    """Widen an empty splice over the whitespace runs that would meet across it.

    Removing ``B.`` from ``A. B. C.`` leaves ``A.  C.`` — two spaces — because
    the span never included either separator. The two runs now touching are worth
    one separator, so they collapse to the strongest break *either side already
    carried*. The strengths are measured on each run separately and then maxed,
    never on the two concatenated: joining the single newlines around a deleted
    line of dialogue would spell ``\\n\\n`` and silently promote the seam to a
    paragraph break the draft never had. At the very start or end of the draft
    there is no second side to separate from, so the run simply goes.
    """
    lead = start
    while lead > 0 and draft[lead - 1].isspace():
        lead -= 1
    trail = end
    while trail < len(draft) and draft[trail].isspace():
        trail += 1
    if lead == 0 or trail == len(draft):
        return lead, trail, ""
    strength = max(_separator_strength(draft[lead:start]), _separator_strength(draft[end:trail]))
    return lead, trail, _SEPARATORS[strength]


# ── Entry point ───────────────────────────────────────────────────────────────


def heal_replacement(draft: str, start: int, end: int, replace: str) -> HealedPatch:
    """Repair one ``replace`` against the draft text on either side of ``[start:end]``.

    *draft* must be the text the splice will actually land in — under
    back-to-front application that is the partially patched draft, whose tail is
    already final. Returns the span to splice and the text to splice into it, or
    a ``rejection`` phrase (in the caller's error vocabulary, no ``Error:``
    prefix and no trailing period) when nothing new survives the repair.
    """
    text = replace.strip()
    spans = _unit_spans(text)
    keys = [_key(text[s:e]) for s, e in spans]
    notes: list[str] = []
    lo, hi = 0, len(spans)

    trailing = _tail_repeat(keys, _neighbour_keys(draft[end:]))
    if trailing:
        hi -= trailing
        notes.append(f"trimmed {trailing} trailing sentence(s) copied from the draft after the span")
    leading = _head_repeat(keys[lo:hi], _neighbour_keys(draft[:start]))
    if leading:
        lo += leading
        notes.append(f"trimmed {leading} leading sentence(s) copied from the draft before the span")
    if lo or hi < len(spans):
        text = text[spans[lo][0] : spans[hi - 1][1]].strip() if lo < hi else ""

    if text:
        return HealedPatch(start, end, text, tuple(notes))

    if replace.strip():
        return HealedPatch(
            start,
            end,
            replace,
            tuple(notes),
            rejection="only repeats text that already surrounds the flagged span — send new prose for the flagged text itself",
        )

    # A deliberate deletion (the model sent an empty `replace`), so close the gap.
    new_start, new_end, separator = _collapse_deletion_seam(draft, start, end)
    if (new_start, new_end) != (start, end):
        notes.append("closed the whitespace gap the deletion left")
    return HealedPatch(new_start, new_end, separator, tuple(notes))
