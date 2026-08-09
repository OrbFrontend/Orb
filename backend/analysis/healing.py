"""healing.py — Repair for editor patches that restate the draft around them.

:func:`analysis.patching.apply_id_patches` splices the model's ``replace`` text
over the exact offsets the audit recorded, so a patch is only as good as the
model's aim. Three mis-aims recur across providers, and all three turn into
visible duplication the moment the splice lands:

* **Restating the sentence before.** ``"I'm bored." She murmured.`` with the
  dialogue tag flagged comes back as ``"I'm bored."`` — the model meant to drop
  the tag, and expressed it by rewriting the line it was *not* asked to touch.
  Spliced verbatim the draft reads ``"I'm bored." "I'm bored."``.
* **Swallowing the sentence after.** ``The air smelled like ozone. The only
  sound was the wind.`` with the first sentence flagged comes back as ``The air
  was crisp. The only sound was the wind.`` — the tail copies text that is still
  sitting in the draft, so the splice prints it twice.
* **Restating the lead-in of the flagged sentence's own line.** Most spans stop
  at a block boundary rather than a sentence boundary: flagging the dialogue tag
  in ``"I am not... flaky," I choke out, my voice small and thin.`` targets the
  narration alone. The model answers with the whole line — ``"I am not...
  flaky," I gasp, the words strained and thin.`` — and the splice prints the
  dialogue twice, because only the tag was ever cut out.

All three are one defect: the replacement carries text that duplicates the draft
*outside* the target span. That text was never in scope, it is already in the
draft, and a copy of it is therefore never new prose — so it can always be
dropped. Healing trims the duplicated run off either end of the replacement and
closes the whitespace a trim (or a deliberate deletion) strands.

**Trimming an end-aligned copy cannot change what the model wrote.** Split the
draft as ``P + L`` before the span and ``T + S`` after it; a replacement that
restates its context is ``L + M + T``, and splicing it verbatim yields
``P L·L M T·T S`` — each copied run printed twice. Splicing the trimmed ``M``
yields ``P L M T S``: the model's own line, read once. That identity is what
licenses the trim, and it holds only while the overlap is exact and anchored to
the ends. An overlap found loose in the middle would not rejoin, and a merely
*similar* neighbour would be prose the model wrote on purpose — "He nodded." and
"She nodded." score 0.95 on any similarity ratio, so nothing here is fuzzy: only
an unchanged, adjacent copy can be removed.

**The unit of comparison is the word, not the sentence.** The third mis-aim
above stops mid-sentence — the copy ends exactly where the flagged span begins,
which is a clause boundary far more often than a sentence boundary. Case and
whitespace are normalised away; punctuation and emphasis markers stay
significant, since ``The wind howled.`` and ``The wind howled!`` are different
text and only an unchanged copy may be trimmed.

**Healing never deletes on the model's behalf.** A replacement that is *entirely*
restated context contributes no new prose at all, so the patch is rejected as a
no-op and the caller feeds the reason back — the same contract as a ``replace``
that repeats the flagged text unchanged. Splicing the empty string instead would
delete the flagged sentence on a guess at intent, and ``targets.py`` treats
silent data loss as the hazard this whole path is designed against.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .text.text_segmentation import HARD_LINE_BREAK_RE, PARA_SPLIT

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


# ── Word comparison ───────────────────────────────────────────────────────────


_WORD_RE = re.compile(r"\S+")


def _word_spans(text: str) -> list[tuple[int, int]]:
    """Offsets of each whitespace-delimited word within *text*.

    Trimming works on offsets rather than on the word strings because rejoining
    them would flatten the separators: a replacement spanning two paragraphs
    would come back on one line. Slicing between two spans keeps whatever the
    model wrote in between, byte for byte.
    """
    return [match.span() for match in _WORD_RE.finditer(text)]


def _key(word: str) -> str:
    """Comparison key for "this is the same word".

    Case only — whitespace cannot survive the tokenizer. Punctuation and
    emphasis markers stay significant: ``howled.`` and ``howled!`` are different
    words, and the whole guard against over-trimming is that only an unchanged
    copy matches.
    """
    return word.casefold()


def _neighbour_keys(text: str) -> list[str]:
    """Comparison keys for the draft on one side of the span, nearest last/first.

    Punctuation-only words are dropped. A target span excludes the emphasis
    markers wrapping it (``audit._strip_markers``), so the draft left behind can
    end in a dangling ``*`` — as its own word that fragment would sit between the
    replacement and the text it actually duplicates, and hide the repeat.
    """
    return [_key(word) for word in _WORD_RE.findall(text) if any(ch.isalnum() for ch in word)]


def _tail_repeat(keys: Sequence[str], following: Sequence[str]) -> int:
    """How many trailing words of the replacement repeat the draft ahead of it.

    Longest overlap first: a replacement ending ``[A, B]`` in front of a draft
    starting ``[A, B, C]`` repeats *two* words, and testing k=1 first would
    compare ``B`` against ``A``, miss, and leave both duplicated.
    """
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
    spans = _word_spans(text)
    keys = [_key(text[s:e]) for s, e in spans]
    notes: list[str] = []
    lo, hi = 0, len(spans)

    trailing = _tail_repeat(keys, _neighbour_keys(draft[end:]))
    if trailing:
        hi -= trailing
        notes.append(f"trimmed {trailing} trailing word(s) copied from the draft after the span")
    # Measured against what the tail trim left, so a replacement that is *all*
    # restated context is not counted twice and still heals down to nothing.
    leading = _head_repeat(keys[lo:hi], _neighbour_keys(draft[:start]))
    if leading:
        lo += leading
        notes.append(f"trimmed {leading} leading word(s) copied from the draft before the span")
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
