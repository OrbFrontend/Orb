"""
text_segmentation.py — Shared paragraph, sentence, and dialogue segmentation
used by all detectors.

Every detector used to carry its own copy of these helpers, and they had silently
drifted apart: some treated "…" as a sentence terminator and some didn't; some
recognized single curly quotes and some didn't; one was missing the
trailing-marker tolerance the others had. This module is the single source of
truth so all detectors split text the same way.

Two splitting functions serve two families of consumers:

- split_sentences — keeps dialogue intact. Used by detectors that need to match
  inside quotes (slop_detector) or analyze clause grammar (contrastive_negation).
- split_narration_sentences — strips dialogue first. Used by detectors that only
  care about narration prose (opening_monotony, template_repetition,
  phrase_repetition).

find_quote_spans and count_sentences support structural_repetition, which
classifies blocks rather than stripping them but still needs the same quote and
terminator definitions.

strip_ooc and ends_with_question support anti_echo: strip_ooc removes the
[OOC: ...] directives that aren't in-character speech, and ends_with_question
classifies a sentence by its terminator (using the same trailing-marker
tolerance as SENT_SPLIT).
"""

from __future__ import annotations

import re

__all__ = [
    "PARA_SPLIT",
    "SENT_SPLIT",
    "OPEN_QUOTES",
    "CLOSE_QUOTES",
    "TOGGLE_QUOTES",
    "EMPHASIS_RE",
    "split_paragraphs",
    "split_sentences",
    "extract_narration",
    "split_narration_sentences",
    "strip_ooc",
    "find_quote_spans",
    "find_emphasis_spans",
    "extract_block_spans",
    "extract_blocks",
    "count_sentences",
    "ends_with_question",
]


# ---------- canonical patterns ----------

# Paragraph break: a blank line, optionally filled with whitespace.
PARA_SPLIT = re.compile(r"\n\s*\n")

# Sentence boundary: a terminator (. ! ? …) followed by whitespace. Trailing
# closing markers (quotes, markdown emphasis, brackets) between the terminator
# and the whitespace are tolerated — e.g. Hiro.* or done." — so a terminator
# hidden behind a markdown marker still splits correctly rather than merging
# adjacent sentences into one.
SENT_SPLIT = re.compile(r"(?<=[.!?…])[\"\u201d\u2019'*_)\]]*\s+")

# Curly directional quotes are unambiguous: left opens, right closes.
OPEN_QUOTES = frozenset({"\u201c", "\u2018"})  # " '
CLOSE_QUOTES = frozenset({"\u201d", "\u2019"})  # " '
# Straight double quote has no direction; we toggle on each occurrence.
# The straight single quote is intentionally excluded from every set so that
# contractions like I'm and don't survive. U+2019 also doubles as a typographic
# apostrophe; extract_narration keeps it intact when it sits between two word
# characters (castle's, don't) and only treats it as a closing quote otherwise.
TOGGLE_QUOTES = frozenset({'"'})


# ---------- paragraph / sentence splitting (dialogue preserved) ----------


def split_paragraphs(text: str) -> list[str]:
    """Split on blank-line paragraph breaks, dropping empty paragraphs."""
    return [p for p in PARA_SPLIT.split(text.strip()) if p.strip()]


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, keeping dialogue intact.

    Splitting is paragraph-first: a paragraph whose final sentence has no
    detectable terminator can't bleed into the next paragraph.
    """
    sentences: list[str] = []
    for para in split_paragraphs(text):
        for raw in SENT_SPLIT.split(para):
            s = raw.strip()
            if s:
                sentences.append(s)
    return sentences


# ---------- dialogue stripping ----------


def extract_narration(paragraph: str) -> str:
    """Return only the text from paragraph that falls outside any quoted span.

    The caller splits into paragraphs first, so quote state resets at each
    paragraph boundary — an unclosed quote inside one paragraph can't bleed
    into the next. Spaces are inserted where quotes are stripped to prevent
    adjacent words from fusing.
    """
    out: list[str] = []
    inside = False
    prev_was_quote = False

    for i, ch in enumerate(paragraph):
        # U+2019 doubles as the typographic apostrophe. When it sits between two
        # word characters (don't, castle's, it's) it's a contraction/possessive,
        # not a closing single-quote — keep it so the word survives intact rather
        # than being clipped to "castle s".
        is_apostrophe = (
            ch == "’" and i > 0 and paragraph[i - 1].isalnum() and i + 1 < len(paragraph) and paragraph[i + 1].isalnum()
        )
        if not is_apostrophe:
            if ch in TOGGLE_QUOTES:
                inside = not inside
                prev_was_quote = True
                continue
            if ch in OPEN_QUOTES:
                inside = True
                prev_was_quote = True
                continue
            if ch in CLOSE_QUOTES:
                inside = False
                prev_was_quote = True
                continue

        if not inside:
            # Insert a space where a quote was stripped, to prevent fusion.
            if prev_was_quote and out and out[-1] not in " \t\n":
                out.append(" ")
            out.append(ch)
        else:
            # Inside quotes — still guard against fusion at the boundary.
            if out and out[-1] not in " \t\n":
                out.append(" ")

        prev_was_quote = False

    return " ".join("".join(out).split())  # Normalize whitespace


def split_narration_sentences(text: str) -> list[str]:
    """Split text into narration sentences, stripping dialogue in the process.

    Splitting is paragraph-aware so quote state and terminators don't bleed
    across paragraph boundaries.
    """
    sentences: list[str] = []
    for para in split_paragraphs(text):
        narration = extract_narration(para).strip()
        if not narration:
            continue
        for raw in SENT_SPLIT.split(narration):
            s = raw.strip()
            if s:
                sentences.append(s)
    return sentences


# ---------- out-of-character asides ----------

# Out-of-character asides are wrapped in square brackets in this app (the system
# itself emits ``[OOC: ...]`` directives, and users follow the same convention).
# They are instructions *to* the model, not in-character speech — including any
# quotes nested inside them. Matching the innermost brackets and re-running until
# stable clears nested asides without letting a stray inner quote survive.
_OOC_BRACKET_RE = re.compile(r"\[[^\[\]]*\]")


def strip_ooc(text: str) -> str:
    """Remove [OOC: ...] out-of-character asides, replacing each with a space
    so the words on either side don't fuse into one token.

    Used by anti_echo to drop the user's directives (and any quotes nested
    inside them) before reading their in-character dialogue.
    """
    prev = None
    while prev != text:
        prev = text
        text = _OOC_BRACKET_RE.sub(" ", text)
    return text


# ---------- structural helpers ----------


def find_quote_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) character spans of every quoted region, inclusive of
    the quote marks themselves.

    Used by detectors that need quote positions rather than stripped narration
    (e.g. structural_repetition, anti_echo). Uses the same quote definitions as
    extract_narration so the two functions agree on what counts as dialogue.
    """
    spans: list[tuple[int, int]] = []
    inside = False
    start = 0
    for i, ch in enumerate(text):
        if ch in TOGGLE_QUOTES:
            if not inside:
                inside = True
                start = i
            else:
                spans.append((start, i + 1))
                inside = False
        elif ch in OPEN_QUOTES:
            if not inside:
                inside = True
                start = i
        elif ch in CLOSE_QUOTES:
            if inside:
                spans.append((start, i + 1))
                inside = False
    return spans


def count_sentences(text: str) -> int:
    """Count the sentences in a block of text.

    Non-empty text with no terminator counts as 1 (a sentence fragment or short
    imperative). Empty text returns 0.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    pieces = [s.strip() for s in SENT_SPLIT.split(stripped) if s.strip()]
    return len(pieces) if pieces else 1


# Trailing closing markers SENT_SPLIT tolerates after a terminator, plus the
# other terminal punctuation that can ride alongside a ``?`` (e.g. "?!", "??").
_TRAILING_MARKERS = " \t\n\"”’'*_)]}>"
_TRAILING_TERMINALS = "!.…"


def ends_with_question(sentence: str) -> bool:
    """True when the sentence ends with a question mark, allowing for trailing
    closing markers (quotes, markdown emphasis) and adjacent terminals like "?!".

    Used by anti_echo to pick out the interrogative sentences in a draft.
    """
    trimmed = sentence.rstrip(_TRAILING_MARKERS).rstrip(_TRAILING_TERMINALS)
    return trimmed.endswith("?")


# ---------- block extraction (SPEECH / EMPHASIS / NARRATION) ----------
# A paragraph is decomposed into ordered spans of three block types:
#   SPEECH    — a quoted span (dialogue), markers included
#   EMPHASIS  — an *asterisk* or _underscore_ span, markers included
#   NARRATION — everything else (bare prose)
# This is the shared source of truth for structural_repetition (which only needs
# the types/counts) and format_consistency (which needs the raw offsets to splice
# a rewrite). Both go through extract_block_spans so they agree on segmentation.

EMPHASIS_RE = re.compile(
    r"(?<!\w)\*(?!\s)([^*\n]+?)\*(?!\w)"  # *thought*  (not bullet)
    r"|"
    r"(?<!\w)_(?!\s)([^_\n]+?)_(?!\w)",  # _thought_
)


def find_emphasis_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of *asterisk* / _underscore_ emphasis runs,
    inclusive of the markers. A leading ``*`` that is the first non-space on its
    line and followed by a space is treated as a markdown bullet and skipped."""
    spans = []
    for m in EMPHASIS_RE.finditer(text):
        if m.group(0).startswith("*"):
            line_start = text.rfind("\n", 0, m.start()) + 1
            prefix = text[line_start : m.start()]
            after_star = m.start() + 1
            if prefix.strip() == "" and after_star < len(text) and text[after_star] in " \t":
                continue
        spans.append((m.start(), m.end()))
    return spans


def _start_inside(start: int, spans: list[tuple[int, int]]) -> bool:
    return any(qs <= start < qe for qs, qe in spans)


def extract_block_spans(para: str) -> list[tuple[str, int, int]]:
    """Decompose a paragraph into contiguous, ordered (block_type, start, end)
    spans that fully tile the paragraph (NARRATION fills the gaps).

    Offsets are raw — no stripping — so ``"".join(para[s:e] ...)`` reconstructs the
    paragraph exactly, which is what lets a rewriter splice individual spans
    without disturbing the surrounding whitespace.

    Emphasis is scanned over the whole paragraph (so the bullet guard keeps its
    line context) and then any emphasis falling inside a quoted span is dropped —
    a ``*`` inside dialogue is never treated as emphasis.
    """
    quote_spans = find_quote_spans(para)
    emphasis_spans = [(s, e) for s, e in find_emphasis_spans(para) if not _start_inside(s, quote_spans)]

    typed = sorted([(s, e, "SPEECH") for s, e in quote_spans] + [(s, e, "EMPHASIS") for s, e in emphasis_spans])

    spans: list[tuple[str, int, int]] = []
    idx = 0
    for s, e, typ in typed:
        if s < idx:  # overlap guard (e.g. emphasis straddling a quote boundary)
            continue
        if idx < s:
            spans.append(("NARRATION", idx, s))
        spans.append((typ, s, e))
        idx = e
    if idx < len(para):
        spans.append(("NARRATION", idx, len(para)))
    return spans


def extract_blocks(para: str) -> list[tuple[str, str]]:
    """Break a paragraph into ordered (block_type, text) pairs with text stripped.

    Block types are SPEECH (quoted dialogue), EMPHASIS (*thought* or _thought_),
    and NARRATION (everything else). Empty (whitespace-only) blocks are dropped.
    """
    blocks: list[tuple[str, str]] = []
    for typ, s, e in extract_block_spans(para):
        t = para[s:e].strip()
        if t:
            blocks.append((typ, t))
    return blocks
