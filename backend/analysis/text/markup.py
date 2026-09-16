"""Read a message's roleplay markup: its conventions, its narration, its speech."""

from __future__ import annotations

import re
from collections.abc import Iterator

from ...core.text_segmentation import (
    PROTECTED_MARKUP_RE,
    extract_unquoted_text,
    map_prose,
    split_paragraphs,
    strip_protected_markup,
)
from .roleplay import (
    THOUGHT_ATTRIBUTION,
    AxisStyle,
    Dialogue,
    Narration,
    emphasis_inner,
    is_inline_emphasis,
    strip_quotes,
)
from .roleplay_segmentation import (
    extract_block_spans,
    find_emphasis_spans,
)

__all__ = [
    "classify_axes",
    "narration_only",
    "protected_runs",
    "spoken_lines",
]


_NARR_HIGH = 0.6  # >= this fraction of narration chars inside *asterisks* -> ASTERISK
_NARR_LOW = 0.25  # <= this -> BARE


# Core owns the shared pattern that excludes Markdown formatting from markup.
_PROTECTED = PROTECTED_MARKUP_RE


# Bare dialogue needs an action beat and unmarked text without clear narration cues.
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
    for paragraph in split_paragraphs(strip_protected_markup(text)):
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
                if is_inline_emphasis(spans, i, para):
                    continue  # inline emphasis is orthogonal to both axes
                block_emph_chars += length
                has_action_beat = has_action_beat or _is_action_beat(emphasis_inner(raw))
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


def _remove_attributed_thoughts(text: str) -> str:
    def strip_segment(para: str) -> str:
        spans = extract_block_spans(para)
        cuts = [
            (start, end)
            for i, (typ, start, end) in enumerate(spans)
            if typ == "EMPHASIS" and not is_inline_emphasis(spans, i, para) and THOUGHT_ATTRIBUTION.match(para[end:])
        ]
        for start, end in reversed(cuts):
            para = para[:start] + " " + para[end:]
        return para

    return map_prose(text, strip_segment)


def _canonical_emphasis(text: str) -> str:
    def normalize(segment: str) -> str:
        for start, end in reversed(find_emphasis_spans(segment)):
            # V2 recognizes asterisks more reliably; avoid emoticons that resemble spans.
            if segment[start] == "_" and segment[start + 1].isalpha():
                segment = segment[:start] + "*" + segment[start + 1 : end - 1] + "*" + segment[end:]
        return segment

    return map_prose(text, normalize)


def narration_only(text: str, dialogue: Dialogue) -> str:
    """Return classifier narration, with speech removed and emphasis canonicalized."""
    if dialogue != Dialogue.BARE:
        return _canonical_emphasis(extract_unquoted_text(_remove_attributed_thoughts(text)))

    narration: list[str] = []
    for paragraph in split_paragraphs(text):
        cleaned = _remove_attributed_thoughts(paragraph)
        if cleaned != paragraph:
            # A thought tag makes the surrounding prose narration.
            narration.append(extract_unquoted_text(cleaned))
            continue
        for para, spans in _paragraph_spans(paragraph):
            for i, (typ, start, end) in enumerate(spans):
                if typ == "EMPHASIS" and not is_inline_emphasis(spans, i, para):
                    inner = emphasis_inner(para[start:end])
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
            inner = " ".join(strip_quotes(para[start:end]).split())
            if inner:
                lines.append(inner)
    return lines
