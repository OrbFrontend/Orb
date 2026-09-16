"""Choose a markup baseline and repair a draft that drifted from it."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from ..toolkit import (
    CLOSE_QUOTES,
    OPEN_QUOTES,
    TOGGLE_QUOTES,
    AxisStyle,
    Dialogue,
    Narration,
    classify_axes,
    emphasis_inner,
    extract_block_spans,
    find_emphasis_spans,
    find_quote_spans,
    map_prose,
    span_role,
    split_ws,
    strip_protected_markup,
    strip_quotes,
)

__all__ = [
    "FormatDriftReport",
    "baseline_axes",
    "normalize_format",
    "normalize_to_baseline",
    "skip_reasons",
    "stable_label",
    "vote_axes",
]


_StyleT = TypeVar("_StyleT", bound=str)


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


def stable_label(values: list[_StyleT], unknown: _StyleT) -> _StyleT:
    """Return the stable majority value, or *unknown*."""
    confident = [v for v in values if v != unknown]
    if not confident:
        return unknown
    val, cnt = Counter(confident).most_common(1)[0]
    if len(confident) == 1 or (cnt >= 2 and cnt / len(confident) >= 0.6):
        return val
    return unknown


def vote_axes(styles: Sequence[AxisStyle]) -> AxisStyle:
    """Vote target axes from several messages' readings, each axis on its own."""
    return AxisStyle(
        dialogue=stable_label([s.dialogue for s in styles], Dialogue.UNKNOWN),
        narration=stable_label([s.narration for s in styles], Narration.UNKNOWN),
    )


def baseline_axes(messages: list[str]) -> AxisStyle:
    """Derive target axes from recent assistant messages."""
    return vote_axes([classify_axes(m) for m in messages if m and m.strip()])


def _wrap_quotes(raw: str) -> str:
    lead, core, trail = split_ws(raw)
    if not core:
        return raw
    core = core.replace("*", "").replace("_", "").strip()
    return f'{lead}"{core}"{trail}'


def _wrap_asterisks(raw: str) -> str:
    lead, core, trail = split_ws(raw)
    if not core:
        return raw
    core = core.replace("*", "").replace("_", "").strip()
    return f"{lead}*{core}*{trail}"


def _strip_block_emphasis(raw: str) -> str:
    """Remove block-emphasis markers and close a bare clause."""
    lead, core, trail = split_ws(raw)
    inner = emphasis_inner(raw)
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
        role = span_role(spans, i, src.dialogue, para)

        if role == "DIALOGUE" and target_dialogue is not None:
            if target_dialogue == Dialogue.BARE and typ == "SPEECH":
                out.append(strip_quotes(raw))
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
        r2 = span_role(spans, j + 1, src.dialogue, para)
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
    # Only unwrap drafts classified as ASTERISK; ambiguous emphasis may be a thought
    # or italic text. Wrap bare narration only when quoted dialogue makes the roles clear.
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

    return map_prose(draft, lambda seg: _rewrite_segment(seg, eff_src, td, tn))


# A stray curly apostrophe ("the dogs’ bowls", "runnin’") is a closing single quote
# to the parser, and it closes nothing, so it cannot swap a parse.
_QUOTE_MARKS = (OPEN_QUOTES | CLOSE_QUOTES | TOGGLE_QUOTES) - {"’"}


def _quote_parse_unreliable(para: str) -> bool:
    """Return whether quote pairing makes this paragraph unsafe to rewrite."""
    spans = find_quote_spans(para)
    for i, ch in enumerate(para):
        if ch not in _QUOTE_MARKS or any(a <= i < b for a, b in spans):
            continue
        if ch in TOGGLE_QUOTES and i > 0 and (para[i - 1].isdigit() or para[i - 1] == "\\"):
            continue  # 5'10" or an escaped quote: the parser skips these too
        return True
    return any(para[a] in TOGGLE_QUOTES and b - a >= 3 and (para[a + 1].isspace() or para[b - 2].isspace()) for a, b in spans)


def _rewrite_segment(text: str, src: AxisStyle, td: Dialogue | None, tn: Narration | None) -> str:
    """Rewrite each safe paragraph in a non-protected segment."""
    return "".join(
        piece if idx % 2 == 1 or not piece.strip() or _quote_parse_unreliable(piece) else _rewrite_paragraph(piece, src, td, tn)
        for idx, piece in enumerate(re.split(r"(\n\s*\n)", text))
    )


# Skip structure and malformed markup that the span parser may misclassify.
_META = re.compile(r"(?m)^[ \t]*[A-Z][A-Z0-9 _/&-]{2,}:(?:[ \t]|$)")
_RULE = re.compile(r"(?m)^[ \t]*(?:-{3,}|={3,}|—{2,})[ \t]*$")
_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+\S")
_TABLE_ROW = re.compile(r"(?m)^[ \t]*\|.*\|[ \t]*$")
# One to three capitalised words, a colon, then speech or a beat: `Cecilia: "..."`.
# `She said: "..."` is prose: its second word is not capitalised.
_SPEAKER = re.compile(r"(?m)^[ \t]*[A-Z][\w'’.-]*(?: [A-Z][\w'’.-]*){0,2}:[ \t]*[\"“*(]")
_LIST_ITEM = re.compile(r"[ \t]*([-+•*]|\d{1,2}[.)])[ \t]+\S")
# A markdown bullet star (`* item` after any hard line break) is never emphasis.
_BULLET_STAR = re.compile(r"(?:^|(?<=[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]))[ \t]*(\*)(?=[ \t])")


def _list_items(text: str) -> int:
    """Count list-item lines, excluding closed star-delimited beats."""
    return sum(
        1 for line in text.splitlines() if (m := _LIST_ITEM.match(line)) and not (m.group(1) == "*" and "*" in line[m.end() :])
    )


def _has_stray_asterisk(text: str) -> bool:
    """Whether a star sits outside every emphasis span and is not a bullet."""
    paired = {i for a, b in find_emphasis_spans(text) for i in range(a, b)}
    paired.update(m.start(1) for m in _BULLET_STAR.finditer(text))
    return any(ch == "*" and i not in paired for i, ch in enumerate(text))


def _quote_in_emphasis(text: str) -> bool:
    """Return whether an emphasis span contains a quote mark."""
    return any(not _QUOTE_MARKS.isdisjoint(text[a:b]) for a, b in find_emphasis_spans(text))


_SKIP_RULES: tuple[tuple[str, Callable[[str], object]], ...] = (
    ("metadata", _META.search),
    ("rule", _RULE.search),
    ("heading", _HEADING.search),
    ("list", lambda text: _list_items(text) >= 2),
    ("table", lambda text: len(_TABLE_ROW.findall(text)) >= 2),
    ("speaker-label", _SPEAKER.search),
    ("stray-asterisk", _has_stray_asterisk),
    ("quote-in-emphasis", _quote_in_emphasis),
)


def skip_reasons(draft: str) -> list[str]:
    """Why *draft* must be left as written (skip-v1); empty when it may be rewritten."""
    visible = strip_protected_markup(draft)
    return [name for name, fires in _SKIP_RULES if fires(visible)]


def normalize_to_baseline(
    draft: str,
    baseline_messages: list[str] | None,
    *,
    enabled: bool,
    target: AxisStyle | None = None,
    source: AxisStyle | None = None,
) -> tuple[str, FormatDriftReport]:
    """Normalize draft markup against recent assistant messages."""
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
    skipped = skip_reasons(draft)
    if skipped:
        return draft, FormatDriftReport(source, target, False, f"skipped ({', '.join(skipped)})")
    new_text = _rewrite(draft, source, target)
    changed = new_text != draft
    note = "normalized" if changed else "already consistent"
    return new_text, FormatDriftReport(source, target, changed, note)
