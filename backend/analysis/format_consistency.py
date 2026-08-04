"""format_consistency.py — Keep RP markup style consistent across messages.

RP output drifts between *formatting conventions*: one message wraps action in
``*asterisks*`` and leaves speech bare, the next leaves action as bare prose and
quotes its speech. This module detects the convention of recent messages and
deterministically rewrites a new draft's markup to match — no LLM involved.

The problem is modelled as two **independent axes**, each detected by coverage
fraction rather than mere presence (so an occasional emphasized word or italic
thought inside otherwise-bare prose does not flip a whole message):

- Dialogue axis — is speech ``QUOTED`` ("…") or ``BARE``?
- Narration axis — is action/narration ``ASTERISK`` (*…*) or ``BARE`` prose?

A span's role (dialogue vs narration) is fixed by its markup plus the message's
convention, so once the axes are known the rewrite is mechanical. When an axis
can't be classified confidently — or the baseline window disagrees — that axis
is left alone, so the safe failure mode is a byte-identical no-op.

Public API:
    classify_axes(text) -> AxisStyle
    baseline_axes(messages) -> AxisStyle
    normalize_format(draft, target) -> str
    normalize_to_baseline(draft, baseline_messages, *, enabled) -> (str, FormatDriftReport)
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from .text.text_segmentation import extract_block_spans, split_paragraphs

__all__ = [
    "Dialogue",
    "Narration",
    "AxisStyle",
    "FormatDriftReport",
    "classify_axes",
    "baseline_axes",
    "normalize_format",
    "normalize_to_baseline",
]


class Dialogue(StrEnum):
    QUOTED = "quoted"
    BARE = "bare"
    UNKNOWN = "unknown"


class Narration(StrEnum):
    ASTERISK = "asterisk"
    BARE = "bare"
    UNKNOWN = "unknown"


_StyleT = TypeVar("_StyleT", Dialogue, Narration)


@dataclass(frozen=True, slots=True)
class AxisStyle:
    dialogue: Dialogue
    narration: Narration

    def label(self) -> str:
        return f"dialogue={self.dialogue.value}, narration={self.narration.value}"


@dataclass(slots=True)
class FormatDriftReport:
    """What the normalizer decided. ``changed`` is True only when the draft text
    was actually rewritten."""

    source: AxisStyle | None
    target: AxisStyle | None
    changed: bool
    note: str

    def transition(self) -> str:
        """``source -> target`` axis labels for logging; ``?`` for an unknown end."""
        src = self.source.label() if self.source else "?"
        tgt = self.target.label() if self.target else "?"
        return f"{src} -> {tgt}"


# ---------- thresholds ----------
# Coverage fractions for the narration axis. Between LOW and HIGH the message is
# genuinely mixed, so we report UNKNOWN and leave it untouched.
_NARR_HIGH = 0.6  # >= this fraction of narration chars inside *asterisks* -> ASTERISK
_NARR_LOW = 0.25  # <= this -> BARE


# ---------- inline emphasis vs block narration ----------


def _emphasis_inner(raw: str) -> str:
    """Strip the surrounding * / _ markers from an emphasis span's raw text."""
    core = raw.strip()
    if len(core) >= 2 and core[0] in "*_" and core[-1] == core[0]:
        return core[1:-1].strip()
    return core.strip("*_ ").strip()


_SENTENCE_END = ".!?…"


def _is_inline_emphasis(spans: list[tuple[str, int, int]], i: int, para: str) -> bool:
    """Is the emphasis span at index *i* inline word-emphasis (an emphasized word,
    ``she was *really* nervous``) rather than block-level action narration
    (``*she tilts her head*``)? Inline emphasis is preserved and never counts
    toward the narration axis; block narration does.

    The distinction is contextual, not lexical. An earlier casing/length heuristic
    (single word, or lowercase first letter -> inline) misread the bulk of real RP
    action beats — ``*smirks*``, ``*leans in close*``, ``*she tilts her head*`` are
    short and/or lowercase yet are narration, not emphasis — so it left the
    asterisk convention undetected and unrewritten.

    Instead: an emphasized word is *embedded* in a run of bare narration prose, so
    bare prose flows into it on the same line without an intervening sentence
    boundary. A block action beat sits at a paragraph start or just after a closing
    quote, so its left side is a boundary, whitespace, or a finished sentence. We
    read that off the left neighbour: inline iff the preceding span is bare
    narration with real text that does not end on a sentence terminator."""
    if i == 0:
        return False
    ptyp, ps, pe = spans[i - 1]
    if ptyp != "NARRATION":
        return False
    left = para[ps:pe].rstrip()
    if not left.strip():
        return False  # whitespace-only gap (e.g. between a quote and the asterisks)
    return left[-1] not in _SENTENCE_END


# ---------- protected spans (passed through, not reformatted) ----------
# Some runs are verbatim content the normalizer must neither read as RP markup nor
# rewrite — it just carries them through unchanged so they reappear in the output as
# the author wrote them:
#   - ```code``` fences — any ``*`` or quote inside is literal;
#   - runs of 2+ asterisks/underscores (Markdown bold/bold-italic and scene
#     rules) — not single-marker RP action markup. Carving them out keeps them
#     intact instead of letting a rewrite strip or reinterpret their markers.
# Protected spans are handled here, above the paragraph split, because a fence can
# itself contain the blank lines that split would otherwise break it on.

_PROTECTED = re.compile(
    r"```.*?```"  # fenced code (may span lines)
    r"|\*{2,}[^\n]*?\*{2,}"  # **bold** / ***bold-italic*** (one line)
    r"|_{2,}[^\n]*?_{2,}"  # __bold__ / ___bold-italic___ (one line)
    r"|[\*_]{3,}",  # lone scene divider
    re.DOTALL,
)


def _split_protected_segments(text: str) -> list[tuple[bool, str]]:
    """Split *text* into ordered ``(protected, chunk)`` parts that rejoin to *text*
    exactly, each protected run (see ``_PROTECTED``) flagged ``protected=True``."""
    parts: list[tuple[bool, str]] = []
    idx = 0
    for m in _PROTECTED.finditer(text):
        if m.start() > idx:
            parts.append((False, text[idx : m.start()]))
        parts.append((True, m.group(0)))
        idx = m.end()
    if idx < len(text):
        parts.append((False, text[idx:]))
    return parts


def _map_prose(text: str, fn: Callable[[str], str]) -> str:
    """Apply *fn* to each non-protected chunk of *text*, passing protected runs
    through verbatim. The rewrite goes through this so it can never reach inside a
    code block or a multi-marker Markdown run."""
    return "".join(chunk if prot else fn(chunk) for prot, chunk in _split_protected_segments(text))


def _strip_protected(text: str) -> str:
    """Replace protected runs with a single space so their literal markup never sways
    classification (the space keeps the surrounding words from fusing)."""
    return _PROTECTED.sub(" ", text)


# ---------- classification ----------


def classify_axes(text: str) -> AxisStyle:
    """Classify *text* on the dialogue and narration axes by coverage fraction.

    Protected runs (fenced code, multi-marker Markdown) are dropped first: their
    contents are literal, so a ``*`` or quote inside one must not register as RP
    markup."""
    text = _strip_protected(text)
    speech_chars = 0
    block_emph_chars = 0
    bare_chars = 0

    for para in split_paragraphs(text):
        spans = extract_block_spans(para)
        for i, (typ, s, e) in enumerate(spans):
            length = len(para[s:e].strip())
            if length == 0:
                continue
            if typ == "SPEECH":
                speech_chars += length
            elif typ == "EMPHASIS":
                if _is_inline_emphasis(spans, i, para):
                    continue  # inline emphasis is orthogonal to both axes
                block_emph_chars += length
            else:  # NARRATION (bare)
                bare_chars += length

    # Narration axis: of the non-dialogue prose, how much sits inside asterisks?
    narr_total = block_emph_chars + bare_chars
    if narr_total == 0:
        narration = Narration.UNKNOWN  # no narration to judge (e.g. pure dialogue)
    else:
        ratio = block_emph_chars / narr_total
        if ratio >= _NARR_HIGH:
            narration = Narration.ASTERISK
        elif ratio <= _NARR_LOW:
            narration = Narration.BARE
        else:
            narration = Narration.UNKNOWN

    # Dialogue axis: quotes are unambiguous, so any real quoted span -> QUOTED.
    # Otherwise dialogue can only be read as BARE when narration is asterisk-marked
    # (the asterisk convention, where the bare runs are the spoken lines).
    if speech_chars > 0:
        dialogue = Dialogue.QUOTED
    elif narration == Narration.ASTERISK and bare_chars > 0:
        dialogue = Dialogue.BARE
    else:
        dialogue = Dialogue.UNKNOWN

    return AxisStyle(dialogue=dialogue, narration=narration)


def _stable(values: list[_StyleT], unknown: _StyleT) -> _StyleT:
    """The agreed value across a baseline window, or *unknown* if it isn't stable.

    Confident (non-unknown) classifications must form a clear majority. A single
    prior message is trusted (early in a conversation there is nothing else)."""
    confident = [v for v in values if v != unknown]
    if not confident:
        return unknown
    val, cnt = Counter(confident).most_common(1)[0]
    if len(confident) == 1 or (cnt >= 2 and cnt / len(confident) >= 0.6):
        return val
    return unknown


def baseline_axes(messages: list[str]) -> AxisStyle:
    """Derive the target axes from recent assistant messages. Each axis is set
    only when the window agrees on it; otherwise it stays UNKNOWN (not enforced)."""
    styles = [classify_axes(m) for m in messages if m and m.strip()]
    return AxisStyle(
        dialogue=_stable([s.dialogue for s in styles], Dialogue.UNKNOWN),
        narration=_stable([s.narration for s in styles], Narration.UNKNOWN),
    )


# ---------- rewriting ----------

_TERMINATORS = ".!?…,;:"


def _role(spans: list[tuple[str, int, int]], i: int, src_dialogue: Dialogue, para: str) -> str:
    """Map the block span at index *i* to its semantic role under the source
    convention."""
    typ = spans[i][0]
    if typ == "SPEECH":
        return "DIALOGUE"
    if typ == "EMPHASIS":
        return "EMPHASIS_INLINE" if _is_inline_emphasis(spans, i, para) else "NARRATION"
    # bare NARRATION span
    if src_dialogue == Dialogue.BARE:
        return "DIALOGUE"  # asterisk convention: bare runs are spoken lines
    return "NARRATION"


def _split_ws(raw: str) -> tuple[str, str, str]:
    """(leading_ws, core, trailing_ws) so a transform touches only the core."""
    lead = raw[: len(raw) - len(raw.lstrip())]
    trail = raw[len(raw.rstrip()) :]
    return lead, raw.strip(), trail


def _strip_quotes(raw: str) -> str:
    lead, core, trail = _split_ws(raw)
    if len(core) >= 2:
        core = core[1:-1].strip()
    return f"{lead}{core}{trail}"


def _wrap_quotes(raw: str) -> str:
    lead, core, trail = _split_ws(raw)
    if not core:
        return raw
    core = core.replace("*", "").replace("_", "").strip()
    return f'{lead}"{core}"{trail}'


def _wrap_asterisks(raw: str) -> str:
    lead, core, trail = _split_ws(raw)
    if not core:
        return raw
    core = core.replace("*", "").replace("_", "").strip()
    return f"{lead}*{core}*{trail}"


def _strip_block_emphasis(raw: str) -> str:
    """Drop the * / _ markers of a block-level emphasis span, turning it into bare
    prose. A terminal period is added when the freed clause ends on a word so the
    next segment doesn't fuse onto it."""
    lead, core, trail = _split_ws(raw)
    inner = _emphasis_inner(raw)
    if inner and inner[-1].isalnum():
        inner += "."
    return f"{lead}{inner}{trail}"


def _rewrite_paragraph(
    para: str,
    src: AxisStyle,
    target_dialogue: Dialogue | None,
    target_narration: Narration | None,
) -> str:
    """Rewrite one paragraph. ``target_*`` is None for an axis that isn't changing.

    ``wrap`` transforms (BARE -> marked) group consecutive same-role spans — plus
    any inline emphasis between them — so a single utterance/clause becomes one
    wrapped unit rather than several fragments."""
    spans = extract_block_spans(para)
    out: list[str] = []
    i = 0
    n = len(spans)
    while i < n:
        typ, s, e = spans[i]
        raw = para[s:e]
        role = _role(spans, i, src.dialogue, para)

        if role == "DIALOGUE" and target_dialogue is not None:
            if target_dialogue == Dialogue.BARE and typ == "SPEECH":
                out.append(_strip_quotes(raw))
                i += 1
                continue
            if target_dialogue == Dialogue.QUOTED and typ == "NARRATION":
                run_end = _group_run(spans, i, src, "DIALOGUE", para)
                out.append(_wrap_quotes(para[s : spans[run_end][2]]))
                i = run_end + 1
                continue

        if role == "NARRATION" and target_narration is not None:
            if target_narration == Narration.BARE and typ == "EMPHASIS":
                out.append(_strip_block_emphasis(raw))
                i += 1
                continue
            if target_narration == Narration.ASTERISK and typ == "NARRATION":
                run_end = _group_run(spans, i, src, "NARRATION", para)
                out.append(_wrap_asterisks(para[s : spans[run_end][2]]))
                i = run_end + 1
                continue

        out.append(raw)
        i += 1
    return "".join(out)


def _group_run(spans: list[tuple[str, int, int]], i: int, src: AxisStyle, role: str, para: str) -> int:
    """Index of the last span in the maximal run starting at *i* that has the
    given role (inline emphasis is absorbed into the run)."""
    j = i
    while j + 1 < len(spans):
        r2 = _role(spans, j + 1, src.dialogue, para)
        if r2 == role or r2 == "EMPHASIS_INLINE":
            j += 1
        else:
            break
    return j


def normalize_format(draft: str, target: AxisStyle) -> str:
    """Rewrite *draft* so its markup matches *target*, changing only the axes that
    differ and can be resolved safely. Returns *draft* unchanged when there is
    nothing confident to do."""
    return _rewrite(draft, classify_axes(draft), target)


def _governing_dialogue(src: AxisStyle, target: AxisStyle) -> Dialogue:
    """Which dialogue convention to read the draft's *bare* spans under — are they
    narration (quoted convention) or spoken lines (asterisk convention)?

    A real quoted span in the draft is hard evidence of the quoted convention, so it
    wins outright. With no quotes the draft is ambiguous on its own — a bare run could
    be un-asterisked narration or bare dialogue — and ``classify_axes`` falls back to
    BARE (asterisk convention) whenever there is asterisk narration beside bare text.
    That guess misreads a *full-markup* draft (quoted dialogue + asterisk narration)
    that simply has no dialogue this turn: its bare runs are narration that lost its
    asterisks, not dialogue. So when the baseline establishes full markup and the
    draft already shows the asterisk-narration half, read it as full markup too. The
    narration axis is unaffected; only the bare-span role flips from dialogue to
    narration, which is what lets a stray bare paragraph be re-wrapped in asterisks
    rather than mistakenly wrapped in quotes."""
    if src.dialogue == Dialogue.QUOTED:
        return Dialogue.QUOTED
    if target.dialogue == Dialogue.QUOTED and target.narration == Narration.ASTERISK and src.narration == Narration.ASTERISK:
        return Dialogue.QUOTED
    return src.dialogue


def _rewrite(draft: str, src: AxisStyle, target: AxisStyle) -> str:
    """Core rewrite, given *draft* already classified as *src*. Split out so the
    ``normalize_to_baseline`` entry point reuses the source it computed for the
    report instead of classifying the same draft twice."""
    # Read the draft's bare spans under the convention the baseline establishes, not
    # the draft's possibly-misleading self-read (see _governing_dialogue).
    eff_dialogue = _governing_dialogue(src, target)
    eff_src = AxisStyle(dialogue=eff_dialogue, narration=src.narration)

    change_dialogue = (
        target.dialogue != Dialogue.UNKNOWN and eff_dialogue != Dialogue.UNKNOWN and target.dialogue != eff_dialogue
    )
    # Enforce a confidently-classified target on *every* span, not only when the
    # draft's dominant style differs. A draft can be dominantly asterisk narration yet
    # still hide a stray bare beat ('"…," she whispered.') or a whole bare paragraph
    # that forgot its asterisks. The rewrite is span-selective and idempotent on already-correct
    # spans, so running it normalizes that within-message drift and leaves the rest
    # byte-identical. The UNKNOWN guard still spares genuinely mixed drafts.
    change_narration = target.narration != Narration.UNKNOWN and src.narration != Narration.UNKNOWN

    # Wrapping bare prose in asterisks means deciding which bare text is narration —
    # only safe when the governing convention says dialogue is quoted.
    if change_narration and target.narration == Narration.ASTERISK and eff_dialogue != Dialogue.QUOTED:
        change_narration = False

    if not (change_dialogue or change_narration):
        return draft

    td = target.dialogue if change_dialogue else None
    tn = target.narration if change_narration else None

    # Rewrite prose only; fenced code blocks pass through verbatim.
    return _map_prose(draft, lambda seg: _rewrite_segment(seg, eff_src, td, tn))


def _rewrite_segment(text: str, src: AxisStyle, td: Dialogue | None, tn: Narration | None) -> str:
    """Rewrite one non-code text segment, paragraph by paragraph.

    Split on paragraph breaks while keeping the original separators, so blank-line
    spacing survives intact (quote/emphasis state already resets per paragraph)."""
    pieces = re.split(r"(\n\s*\n)", text)
    rebuilt = [
        piece if (idx % 2 == 1 or not piece.strip()) else _rewrite_paragraph(piece, src, td, tn)
        for idx, piece in enumerate(pieces)
    ]
    return "".join(rebuilt)


def normalize_to_baseline(
    draft: str,
    baseline_messages: list[str] | None,
    *,
    enabled: bool,
) -> tuple[str, FormatDriftReport]:
    """Entry point: hold *draft* to the convention of *baseline_messages*.

    Returns the (possibly unchanged) text and a small report for logging. A no-op
    — disabled, no baseline, ambiguous axes, or already consistent — returns the
    draft byte-for-byte."""
    if not enabled:
        return draft, FormatDriftReport(None, None, False, "disabled")
    if not draft or not draft.strip() or not baseline_messages:
        return draft, FormatDriftReport(None, None, False, "no baseline")

    target = baseline_axes(baseline_messages)
    if target.dialogue == Dialogue.UNKNOWN and target.narration == Narration.UNKNOWN:
        return draft, FormatDriftReport(None, target, False, "baseline unstable")

    # Protected runs (fenced code, multi-marker Markdown) are excluded from the
    # classification and carried through the rewrite verbatim, so a stray
    # ``***``/``****`` neither corrupts the read nor gets dropped — it reappears in
    # the output exactly as the author typed it.
    source = classify_axes(draft)
    new_text = _rewrite(draft, source, target)
    changed = new_text != draft
    note = "normalized" if changed else "already consistent"
    return new_text, FormatDriftReport(source, target, changed, note)
