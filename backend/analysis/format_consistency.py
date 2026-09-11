"""Keep roleplay markup consistent with recent messages."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from ..core.text_segmentation import PROTECTED_MARKUP_RE
from .text.text_segmentation import (
    CLOSE_QUOTES,
    OPEN_QUOTES,
    TOGGLE_QUOTES,
    extract_block_spans,
    extract_narration,
    find_emphasis_spans,
    find_quote_spans,
    split_paragraphs,
)

__all__ = [
    "Dialogue",
    "Narration",
    "AxisStyle",
    "FormatDriftReport",
    "classify_axes",
    "protected_runs",
    "spoken_lines",
    "narration_only",
    "baseline_axes",
    "stable_label",
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


_StyleT = TypeVar("_StyleT", bound=str)


@dataclass(frozen=True, slots=True)
class AxisStyle:
    dialogue: Dialogue
    narration: Narration

    def label(self) -> str:
        return f"dialogue={self.dialogue.value}, narration={self.narration.value}"


@dataclass(slots=True)
class FormatDriftReport:
    """The normalizer's result; ``changed`` means the draft was rewritten."""

    source: AxisStyle | None
    target: AxisStyle | None
    changed: bool
    note: str

    def transition(self) -> str:
        """Return ``source -> target`` labels for logging."""
        src = self.source.label() if self.source else "?"
        tgt = self.target.label() if self.target else "?"
        return f"{src} -> {tgt}"


_NARR_HIGH = 0.6  # >= this fraction of narration chars inside *asterisks* -> ASTERISK
_NARR_LOW = 0.25  # <= this -> BARE


def _emphasis_inner(raw: str) -> str:
    """Strip surrounding emphasis markers."""
    core = raw.strip()
    if len(core) >= 2 and core[0] in "*_" and core[-1] == core[0]:
        return core[1:-1].strip()
    return core.strip("*_ ").strip()


# A clause can close on stylistic punctuation as well as a full stop: roleplay
# greetings routinely end a line on "~", "♪" or a cut-off dash before an
# action beat. Anything not listed here reads as mid-sentence italics.
_SENTENCE_END = ".!?…~♪♥♡—–-)]"


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


# Fenced code, bold runs and scene dividers are formatting, not RP markup. The
# pattern lives in core so the markup classifier's input shaping
# (`local_ml.markup_input`) hides exactly what this module hides.
_PROTECTED = PROTECTED_MARKUP_RE


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


# Bare dialogue ("*she smiles* Hello") needs an action beat and unmarked text
# without clear third-person narration or speech attribution.
_THOUGHT_MARKERS = frozenset(
    {
        "think",
        "thinks",
        "thought",
        "thinking",
        "know",
        "knows",
        "knew",
        "wonder",
        "wonders",
        "wondered",
        "remember",
        "remembers",
        "remembered",
        "realize",
        "realizes",
        "realized",
        "believe",
        "believes",
        "believed",
        "understand",
        "understands",
        "understood",
        "suppose",
        "supposes",
        "guess",
        "guesses",
        "imagine",
        "imagines",
        "mean",
        "means",
        "meant",
        "feel",
        "feels",
        "felt",
        "want",
        "wants",
        "wanted",
        "wish",
        "wishes",
        "wished",
        "hope",
        "hopes",
        "hoped",
        "love",
        "loves",
        "loved",
        "hate",
        "hates",
        "hated",
        "like",
        "likes",
        "liked",
        "need",
        "needs",
        "needed",
        "afraid",
        "scared",
        "maybe",
        "perhaps",
        "surely",
        "must",
        "should",
        "why",
    }
)
_BARE_NARRATION_MARKERS = frozenset(
    {
        "he",
        "him",
        "his",
        "she",
        "her",
        "hers",
        "himself",
        "herself",
        "they",
        "them",
        "their",
        "theirs",
        "said",
        "says",
        "asked",
        "asks",
        "replied",
        "replies",
        "answered",
        "answers",
        "murmured",
        "murmurs",
        "whispered",
        "whispers",
        "muttered",
        "mutters",
        "shouted",
        "shouts",
        "called",
        "calls",
        "added",
        "adds",
    }
)

_WORD = re.compile(r"[a-z']+")


def _paragraph_spans(text: str) -> Iterator[tuple[str, list[tuple[str, int, int]]]]:
    """Yield rewriteable paragraphs with their typed spans."""
    for paragraph in split_paragraphs(_strip_protected(text)):
        yield paragraph, extract_block_spans(paragraph)


def _is_action_beat(beat: str) -> bool:
    """Return whether block emphasis looks like a stage direction."""
    words = _WORD.findall(beat.lower())
    return len(words) >= 2 and _THOUGHT_MARKERS.isdisjoint(words)


def classify_axes(text: str) -> AxisStyle:
    """Classify dialogue and narration markup by coverage."""
    speech_chars = 0
    block_emph_chars = 0
    bare_chars = 0
    has_action_beat = False
    bare_words: set[str] = set()

    for para, spans in _paragraph_spans(text):
        for i, (typ, s, e) in enumerate(spans):
            raw = para[s:e]
            length = len(raw.strip())
            if length == 0:
                continue
            if typ == "SPEECH":
                speech_chars += length
            elif typ == "EMPHASIS":
                if _is_inline_emphasis(spans, i, para):
                    continue  # inline emphasis is orthogonal to both axes
                block_emph_chars += length
                has_action_beat = has_action_beat or _is_action_beat(_emphasis_inner(raw))
            else:  # NARRATION (bare)
                bare_chars += length
                bare_words.update(_WORD.findall(raw.lower()))

    narr_total = block_emph_chars + bare_chars
    if narr_total == 0:
        narration = Narration.UNKNOWN
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
    elif bare_chars > 0 and has_action_beat and bare_words.isdisjoint(_BARE_NARRATION_MARKERS):
        dialogue = Dialogue.BARE
        narration = Narration.ASTERISK
    else:
        dialogue = Dialogue.UNKNOWN

    return AxisStyle(dialogue=dialogue, narration=narration)


_THOUGHT_ATTRIBUTION = re.compile(
    r"\s*(?:I|[Hh]e|[Ss]he|[Tt]hey|[Ww]e|[Yy]ou|[Tt]he\s+[\w'-]+|[A-Z][\w'-]*)\s+"
    r"(?:thinks?|thought|(?:tells?|told|says?|said|reminds?|reminded|asks?|asked)\s+"
    r"(?:myself|himself|herself|themselves|ourselves|yourself))"
    r"(?=\s*[,.;!?]|\s+(?:with|as|while|\w+ly)\b|\s*$)"
)


def _remove_attributed_thoughts(text: str) -> str:
    def strip_segment(para: str) -> str:
        spans = extract_block_spans(para)
        cuts = [
            (start, end)
            for i, (typ, start, end) in enumerate(spans)
            if typ == "EMPHASIS" and not _is_inline_emphasis(spans, i, para) and _THOUGHT_ATTRIBUTION.match(para[end:])
        ]
        for start, end in reversed(cuts):
            para = para[:start] + " " + para[end:]
        return para

    return _map_prose(text, strip_segment)


def _canonical_emphasis(text: str) -> str:
    def normalize(segment: str) -> str:
        for start, end in reversed(find_emphasis_spans(segment)):
            # V2 recognizes asterisks more reliably. Exclude emoticons such as
            # two occurrences of (⌐■_■), which can resemble an emphasis span.
            if segment[start] == "_" and segment[start + 1].isalpha():
                segment = segment[:start] + "*" + segment[start + 1 : end - 1] + "*" + segment[end:]
        return segment

    return _map_prose(text, normalize)


def narration_only(text: str, dialogue: Dialogue) -> str:
    """Return classifier narration, with speech removed and emphasis canonicalized."""
    if dialogue != Dialogue.BARE:
        return _canonical_emphasis(extract_narration(_remove_attributed_thoughts(text)))

    narration: list[str] = []
    for paragraph in split_paragraphs(text):
        cleaned = _remove_attributed_thoughts(paragraph)
        if cleaned != paragraph:
            # A thought tag identifies surrounding prose as narration. Keep that
            # context without changing how other paragraphs' bare speech is read.
            narration.append(extract_narration(cleaned))
            continue
        for para, spans in _paragraph_spans(paragraph):
            for i, (typ, start, end) in enumerate(spans):
                if typ == "EMPHASIS" and not _is_inline_emphasis(spans, i, para):
                    inner = _emphasis_inner(para[start:end])
                    if inner:
                        narration.append(inner)
    return _canonical_emphasis(" ".join(narration))


def protected_runs(text: str) -> list[str]:
    """Return fenced code, bold markup, and scene-divider runs."""
    return [m.group(0) for m in _PROTECTED.finditer(text)]


def spoken_lines(text: str) -> list[str]:
    """Return the contents of quoted spans with quotes and spacing normalized."""
    lines: list[str] = []
    for para, block in _paragraph_spans(text):
        for typ, start, end in block:
            if typ != "SPEECH":
                continue
            inner = " ".join(_strip_quotes(para[start:end]).split())
            if inner:
                lines.append(inner)
    return lines


def stable_label(values: list[_StyleT], unknown: _StyleT) -> _StyleT:
    """Return the stable majority value, or *unknown*."""
    confident = [v for v in values if v != unknown]
    if not confident:
        return unknown
    val, cnt = Counter(confident).most_common(1)[0]
    if len(confident) == 1 or (cnt >= 2 and cnt / len(confident) >= 0.6):
        return val
    return unknown


def baseline_axes(messages: list[str]) -> AxisStyle:
    """Derive target axes from recent assistant messages."""
    styles = [classify_axes(m) for m in messages if m and m.strip()]
    return AxisStyle(
        dialogue=stable_label([s.dialogue for s in styles], Dialogue.UNKNOWN),
        narration=stable_label([s.narration for s in styles], Narration.UNKNOWN),
    )


_TERMINATORS = ".!?…,;:"


def _role(spans: list[tuple[str, int, int]], i: int, src_dialogue: Dialogue, para: str) -> str:
    """Map a span to its semantic role under the source convention."""
    typ = spans[i][0]
    if typ == "SPEECH":
        return "DIALOGUE"
    if typ == "EMPHASIS":
        return "EMPHASIS_INLINE" if _is_inline_emphasis(spans, i, para) else "NARRATION"
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
                run_end = _group_run(spans, i, src, "NARRATION", para, only_type="NARRATION")
                out.append(_wrap_asterisks(para[s : spans[run_end][2]]))
                i = run_end + 1
                continue

        out.append(raw)
        i += 1
    return "".join(out)


def _group_run(
    spans: list[tuple[str, int, int]],
    i: int,
    src: AxisStyle,
    role: str,
    para: str,
    *,
    only_type: str | None = None,
) -> int:
    """Return the last span in the same-role run starting at *i*."""
    j = i
    while j + 1 < len(spans):
        typ2 = spans[j + 1][0]
        r2 = _role(spans, j + 1, src.dialogue, para)
        if r2 == "EMPHASIS_INLINE":
            j += 1
        elif r2 == role and (only_type is None or typ2 == only_type):
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
    # Bare dialogue plus bare narration is ambiguous, so do not rewrite either axis.
    if target.dialogue == Dialogue.BARE and target.narration == Narration.BARE:
        return draft

    eff_dialogue = _governing_dialogue(src, target)
    eff_src = AxisStyle(dialogue=eff_dialogue, narration=src.narration)

    change_dialogue = (
        target.dialogue != Dialogue.UNKNOWN and eff_dialogue != Dialogue.UNKNOWN and target.dialogue != eff_dialogue
    )
    # Unwrapping converts asterisk narration, so only a draft that reads ASTERISK is
    # unwrapped. Whatever emphasis a bare or unknown draft keeps, the rewriter cannot
    # tell a slip from an italic thought, a sound effect or a stressed word, and
    # unwrapping those turns them into prose. Wrapping is safe only when quoted
    # dialogue identifies bare spans as narration.
    if target.narration == Narration.BARE:
        change_narration = src.narration == Narration.ASTERISK
    elif target.narration == Narration.ASTERISK:
        change_narration = eff_dialogue == Dialogue.QUOTED
    else:
        change_narration = False

    if not (change_dialogue or change_narration):
        return draft

    td = target.dialogue if change_dialogue else None
    tn = target.narration if change_narration else None

    return _map_prose(draft, lambda seg: _rewrite_segment(seg, eff_src, td, tn))


# A stray curly apostrophe ("the dogs’ bowls", "runnin’") is a closing single quote
# to the parser, and it closes nothing, so it cannot swap a parse.
_QUOTE_MARKS = (OPEN_QUOTES | CLOSE_QUOTES | TOGGLE_QUOTES) - {"’"}


def _quote_parse_unreliable(para: str) -> bool:
    """Whether *para*'s quote parse is unsafe to rewrite on.

    ``find_quote_spans`` pairs a straight quote with the NEXT one, so an unclosed
    quote swaps speech and narration for the rest of its paragraph:
    ``"Hello, she said. "Bye," he replied`` parses as the speech
    ``"Hello, she said. "``. A quote mark outside every span, or a straight-quote
    span that opens onto whitespace or closes after it, is that swap. No reading
    of the draft makes such a paragraph safe to rewrite.
    """
    spans = find_quote_spans(para)
    for i, ch in enumerate(para):
        if ch not in _QUOTE_MARKS or any(a <= i < b for a, b in spans):
            continue
        if ch in TOGGLE_QUOTES and i > 0 and (para[i - 1].isdigit() or para[i - 1] == "\\"):
            continue  # 5'10" or an escaped quote: the parser skips these too
        return True
    return any(para[a] in TOGGLE_QUOTES and b - a >= 3 and (para[a + 1].isspace() or para[b - 2].isspace()) for a, b in spans)


def _rewrite_segment(text: str, src: AxisStyle, td: Dialogue | None, tn: Narration | None) -> str:
    """Rewrite a non-protected segment paragraph by paragraph, leaving any
    paragraph with an unreliable quote parse as written."""
    return "".join(
        piece if idx % 2 == 1 or not piece.strip() or _quote_parse_unreliable(piece) else _rewrite_paragraph(piece, src, td, tn)
        for idx, piece in enumerate(re.split(r"(\n\s*\n)", text))
    )


def normalize_to_baseline(
    draft: str,
    baseline_messages: list[str] | None,
    *,
    enabled: bool,
    target: AxisStyle | None = None,
    source: AxisStyle | None = None,
) -> tuple[str, FormatDriftReport]:
    """Normalize draft markup against recent assistant messages.

    *target* and *source* default to the heuristic reading of the baseline and of
    the draft; passing both lets another classifier decide the rewrite end to end.
    """
    if not enabled:
        return draft, FormatDriftReport(None, None, False, "disabled")
    if not draft or not draft.strip() or not baseline_messages:
        return draft, FormatDriftReport(None, None, False, "no baseline")

    if target is None:
        target = baseline_axes(baseline_messages)
    if target.dialogue == Dialogue.UNKNOWN and target.narration == Narration.UNKNOWN:
        return draft, FormatDriftReport(None, target, False, "baseline unstable")

    if source is None:
        source = classify_axes(draft)
    new_text = _rewrite(draft, source, target)
    changed = new_text != draft
    note = "normalized" if changed else "already consistent"
    return new_text, FormatDriftReport(source, target, changed, note)
