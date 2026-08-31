"""Keep roleplay markup consistent with recent messages."""

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


_NARR_HIGH = 0.6  # >= this fraction of narration chars inside *asterisks* -> ASTERISK
_NARR_LOW = 0.25  # <= this -> BARE


def _emphasis_inner(raw: str) -> str:
    """Strip the surrounding * / _ markers from an emphasis span's raw text."""
    core = raw.strip()
    if len(core) >= 2 and core[0] in "*_" and core[-1] == core[0]:
        return core[1:-1].strip()
    return core.strip("*_ ").strip()


_SENTENCE_END = ".!?…"


def _is_inline_emphasis(spans: list[tuple[str, int, int]], i: int, para: str) -> bool:
    """Return whether an emphasis span is inline rather than block narration."""
    if i == 0:
        return False
    ptyp, ps, pe = spans[i - 1]
    if ptyp != "NARRATION":
        return False
    left = para[ps:pe].rstrip()
    if not left.strip():
        return False  # whitespace-only gap (e.g. between a quote and the asterisks)
    return left[-1] not in _SENTENCE_END


_PROTECTED = re.compile(
    r"```.*?```"  # fenced code (may span lines)
    r"|\*{2,}[^\n]*?\*{2,}"  # **bold** / ***bold-italic*** (one line)
    r"|_{2,}[^\n]*?_{2,}"  # __bold__ / ___bold-italic___ (one line)
    r"|[\*_]{3,}",  # lone scene divider
    re.DOTALL,
)


def _split_protected_segments(text: str) -> list[tuple[bool, str]]:
    """Split text into protected and rewriteable chunks."""
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
    """Apply *fn* outside protected runs."""
    return "".join(chunk if prot else fn(chunk) for prot, chunk in _split_protected_segments(text))


def _strip_protected(text: str) -> str:
    """Hide protected runs from classification."""
    return _PROTECTED.sub(" ", text)


def classify_axes(text: str) -> AxisStyle:
    """Classify dialogue and narration markup by coverage."""
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

    if speech_chars > 0:
        dialogue = Dialogue.QUOTED
    elif narration == Narration.ASTERISK and bare_chars > 0:
        dialogue = Dialogue.BARE
    else:
        dialogue = Dialogue.UNKNOWN

    return AxisStyle(dialogue=dialogue, narration=narration)


def _stable(values: list[_StyleT], unknown: _StyleT) -> _StyleT:
    """Return the stable majority value, or *unknown*."""
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
    """Remove block-emphasis markers and close a bare clause."""
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
    """Rewrite one paragraph for the selected markup axes."""
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
    """Return the last span in the same-role run starting at *i*."""
    j = i
    while j + 1 < len(spans):
        r2 = _role(spans, j + 1, src.dialogue, para)
        if r2 == role or r2 == "EMPHASIS_INLINE":
            j += 1
        else:
            break
    return j


def normalize_format(draft: str, target: AxisStyle) -> str:
    """Rewrite draft markup to match *target* where classification is safe."""
    return _rewrite(draft, classify_axes(draft), target)


def _governing_dialogue(src: AxisStyle, target: AxisStyle) -> Dialogue:
    """Choose how bare spans should be interpreted during rewriting."""
    if src.dialogue == Dialogue.QUOTED:
        return Dialogue.QUOTED
    if target.dialogue == Dialogue.QUOTED and target.narration == Narration.ASTERISK and src.narration == Narration.ASTERISK:
        return Dialogue.QUOTED
    return src.dialogue


def _rewrite(draft: str, src: AxisStyle, target: AxisStyle) -> str:
    """Rewrite a draft using an existing source classification."""
    eff_dialogue = _governing_dialogue(src, target)
    eff_src = AxisStyle(dialogue=eff_dialogue, narration=src.narration)

    change_dialogue = (
        target.dialogue != Dialogue.UNKNOWN and eff_dialogue != Dialogue.UNKNOWN and target.dialogue != eff_dialogue
    )
    change_narration = target.narration != Narration.UNKNOWN and src.narration != Narration.UNKNOWN

    if change_narration and target.narration == Narration.ASTERISK and eff_dialogue != Dialogue.QUOTED:
        change_narration = False

    if not (change_dialogue or change_narration):
        return draft

    td = target.dialogue if change_dialogue else None
    tn = target.narration if change_narration else None

    return _map_prose(draft, lambda seg: _rewrite_segment(seg, eff_src, td, tn))


def _rewrite_segment(text: str, src: AxisStyle, td: Dialogue | None, tn: Narration | None) -> str:
    """Rewrite a non-protected segment paragraph by paragraph."""
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
    """Normalize draft markup against recent assistant messages."""
    if not enabled:
        return draft, FormatDriftReport(None, None, False, "disabled")
    if not draft or not draft.strip() or not baseline_messages:
        return draft, FormatDriftReport(None, None, False, "no baseline")

    target = baseline_axes(baseline_messages)
    if target.dialogue == Dialogue.UNKNOWN and target.narration == Narration.UNKNOWN:
        return draft, FormatDriftReport(None, target, False, "baseline unstable")

    source = classify_axes(draft)
    new_text = _rewrite(draft, source, target)
    changed = new_text != draft
    note = "normalized" if changed else "already consistent"
    return new_text, FormatDriftReport(source, target, changed, note)
