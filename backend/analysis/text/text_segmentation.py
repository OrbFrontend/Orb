"""
text_segmentation.py — Shared paragraph, sentence, and dialogue segmentation
used by all detectors.

This module is the single source of truth so all detectors split text the same
way: one ruling on "…" as a sentence terminator, on single curly quotes, and on
how much trailing punctuation a marker tolerates.

Two splitting functions serve two families of consumers:

- split_sentences — keeps dialogue intact. Used by detectors that analyze
  clause grammar (contrastive_negation).
- split_narration_sentences — strips dialogue first. Used by detectors that only
  care about narration prose (opening_monotony, template_repetition,
  phrase_repetition).
- split_segment_sentences — separates dialogue from narration but keeps both.
  Used by detectors that match inside quotes yet must report snippets that
  never mix the two (slop_detector).

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
from collections.abc import Iterator

__all__ = [
    "PARA_SPLIT",
    "SENT_SPLIT",
    "OPEN_QUOTES",
    "CLOSE_QUOTES",
    "TOGGLE_QUOTES",
    "EMPHASIS_RE",
    "split_paragraphs",
    "split_sentences",
    "sentence_boundary_ends",
    "ends_with_sentence_terminator",
    "split_segment_sentences",
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

# Compatibility regex for the original coarse boundary contract. Public
# splitting/counting now uses the scanner below, which preserves closing markup,
# understands abbreviations, and supports non-ASCII terminators.
SENT_SPLIT = re.compile(r"(?:(?<=[.!?…])[\"\u201d\u2019'*_)\]]*\s+|(?:\r\n|[\n\r\u0085\u2028\u2029])+)")

# Directional pairs cover common English, European, and CJK dialogue marks. A
# closer that is also an opener in another convention closes the current
# matching pair first.
_QUOTE_PAIRS = {
    "“": "”",
    "‘": "’",
    "«": "»",
    "‹": "›",
    "「": "」",
    "『": "』",
    "„": "“",
    "‚": "‘",
}
OPEN_QUOTES = frozenset(_QUOTE_PAIRS)
CLOSE_QUOTES = frozenset(_QUOTE_PAIRS.values())
# Straight double quote has no direction; we toggle on each occurrence.
# The straight single quote is intentionally excluded from every set so that
# contractions like I'm and don't survive. U+2019 also doubles as a typographic
# apostrophe; extract_narration keeps it intact when it sits between two word
# characters (castle's, don't) and only treats it as a closing quote otherwise.
TOGGLE_QUOTES = frozenset({'"'})

_TERMINATORS = frozenset(".!?…。！？؟۔｡．।॥")
_QUESTION_TERMINATORS = frozenset("?？؟")
_TIGHT_TERMINATORS = frozenset("…。！？؟۔｡．।॥")
_TRAILING_MARKERS = frozenset("'»›」』*_)]}>") | CLOSE_QUOTES | TOGGLE_QUOTES

# Periods need lexical context. This deliberately stays conservative: it
# suppresses only forms that are overwhelmingly non-terminal, leaving
# ambiguous abbreviations such as ``etc.`` to end a sentence before a capital.
_ALWAYS_NONTERMINAL_ABBREVIATIONS = frozenset({"e.g.", "i.e.", "a.k.a.", "vs.", "v.", "cf."})
_LOWERCASE_CONTINUATION_ABBREVIATIONS = frozenset(
    {"a.m.", "p.m.", "approx.", "dept.", "est.", "misc.", "incl.", "esp.", "min.", "max.", "ref.", "sec."}
)
_TITLE_ABBREVIATIONS = frozenset(
    {
        "mr.",
        "mrs.",
        "ms.",
        "mx.",
        "dr.",
        "prof.",
        "rev.",
        "hon.",
        "pres.",
        "gov.",
        "sen.",
        "rep.",
        "sr.",
        "jr.",
        "st.",
        "mt.",
        "capt.",
        "cpt.",
        "lt.",
        "col.",
        "gen.",
        "sgt.",
        "adm.",
        "maj.",
    }
)
_NUMBER_ABBREVIATIONS = frozenset(
    {
        "no.",
        "fig.",
        "eq.",
        "ch.",
        "vol.",
        "pp.",
        "jan.",
        "feb.",
        "mar.",
        "apr.",
        "jun.",
        "jul.",
        "aug.",
        "sep.",
        "sept.",
        "oct.",
        "nov.",
        "dec.",
    }
)
_ABBREVIATION_BEFORE_PERIOD = re.compile(r"(?:[^\W\d_]+\.)+$", re.UNICODE)


# ---------- paragraph / sentence splitting (dialogue preserved) ----------


def _period_is_nonterminal(text: str, period: int, next_char: int) -> bool:
    """Whether ``text[period]`` belongs to an abbreviation rather than ending
    a sentence. Decimal points never reach this helper because they have no
    following whitespace, but the explicit check makes the contract robust for
    callers that feed unusual spaced numeric forms."""
    if period > 0 and period + 1 < len(text) and text[period - 1].isdigit() and text[period + 1].isdigit():
        return True

    match = _ABBREVIATION_BEFORE_PERIOD.search(text[: period + 1])
    if match is None:
        return False
    raw_abbreviation = match.group(0)
    abbreviation = raw_abbreviation.casefold()
    next_value = text[next_char] if next_char < len(text) else ""

    if abbreviation in _ALWAYS_NONTERMINAL_ABBREVIATIONS:
        return True
    if abbreviation in _TITLE_ABBREVIATIONS and next_value.isalnum():
        return True
    if abbreviation in _NUMBER_ABBREVIATIONS and next_value.isdigit():
        return True

    letters = raw_abbreviation.replace(".", "")
    # Initials and uppercase acronyms before a following name/noun: ``J. R. R.
    # Tolkien`` and ``U.S. Army``. Lowercase ``a.m. She`` remains a boundary.
    if next_value.isalnum() and (len(letters) == 1 or (abbreviation.count(".") >= 2 and letters.isupper())):
        return True

    # Explicit ambiguous abbreviations continue only before lowercase prose
    # (``approx. three hours``); ``It was 5 p.m. She left.`` still splits.
    return abbreviation in _LOWERCASE_CONTINUATION_ABBREVIATIONS and next_value.islower()


def sentence_boundary_ends(text: str) -> Iterator[int]:
    """Yield exclusive ends of complete sentence boundaries in *text*.

    Each end includes trailing quote/emphasis/bracket markers and the separator
    whitespace. This makes ``text[start:end]`` lossless for document-tail
    trimming while split helpers can simply ``strip()`` each unit. Periods in
    common abbreviations/initials are suppressed.
    """
    i = 0
    size = len(text)
    while i < size:
        if text[i] not in _TERMINATORS:
            i += 1
            continue

        terminal_end = i + 1
        while terminal_end < size and text[terminal_end] in _TERMINATORS:
            terminal_end += 1

        marker_end = terminal_end
        while marker_end < size and text[marker_end] in _TRAILING_MARKERS:
            marker_end += 1

        boundary_end = marker_end
        while boundary_end < size and text[boundary_end].isspace():
            boundary_end += 1
        has_separator = boundary_end > marker_end
        allows_tight_boundary = any(ch in _TIGHT_TERMINATORS for ch in text[i:terminal_end])
        if not has_separator and not (allows_tight_boundary and marker_end < size):
            i = terminal_end
            continue

        only_one_period = terminal_end == i + 1 and text[i] == "."
        if not (only_one_period and _period_is_nonterminal(text, i, boundary_end)):
            yield boundary_end
            i = boundary_end
        else:
            i = terminal_end


def _split_sentence_line(text: str) -> list[str]:
    units: list[str] = []
    start = 0
    for end in sentence_boundary_ends(text):
        unit = text[start:end].strip()
        if unit:
            units.append(unit)
        start = end
    tail = text[start:].strip()
    if tail:
        units.append(tail)
    return units


def _split_sentence_units(text: str) -> list[str]:
    """Split punctuation-delimited units with every line break as a hard edge.

    ``str.splitlines`` covers LF, CRLF, bare CR, and Unicode line separators.
    Line endings are separators rather than sentence content, so no returned
    unit can contain one even when neither adjacent line has punctuation.
    """
    units: list[str] = []
    for line in text.splitlines():
        units.extend(_split_sentence_line(line))
    return units


def ends_with_sentence_terminator(text: str) -> bool:
    """True when *text* ends in sentence punctuation, allowing trailing
    closing quote/emphasis/bracket markers and whitespace."""
    trimmed = text.rstrip()
    while trimmed and trimmed[-1] in _TRAILING_MARKERS:
        trimmed = trimmed[:-1].rstrip()
    return bool(trimmed and trimmed[-1] in _TERMINATORS)


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
        sentences.extend(_split_sentence_units(para))
    return sentences


# ---------- dialogue stripping ----------


def extract_narration(paragraph: str) -> str:
    """Return only the text from paragraph that falls outside any quoted span.

    The caller splits into paragraphs first. Only balanced quoted spans are
    removed; an unmatched opener is literal prose rather than a reason to drop
    the rest of the paragraph. Spaces prevent adjacent words from fusing.
    """
    pieces: list[str] = []
    cursor = 0
    for start, end in find_quote_spans(paragraph):
        pieces.append(paragraph[cursor:start])
        pieces.append(" ")  # Prevent the words on either side from fusing.
        cursor = end
    pieces.append(paragraph[cursor:])
    return " ".join("".join(pieces).split())


def split_narration_sentences(text: str) -> list[str]:
    """Split text into narration sentences, stripping dialogue in the process.

    Splitting is paragraph-aware so quote state and terminators don't bleed
    across paragraph boundaries. Narration runs on opposite sides of dialogue
    are kept separate rather than fused into a synthetic string that never
    appeared in the source; every returned fragment is therefore a contiguous
    substring suitable for an editor search/replace patch.
    """
    sentences: list[str] = []

    def append_run(run: str, *, touches_dialogue: bool) -> None:
        units = _split_sentence_units(run)
        if touches_dialogue:
            # In punctuation-outside-quote styles (``"Enough". Then``), the
            # period is a narration block of its own. It is quote punctuation,
            # not a prose sentence and must not break an opener run.
            units = [unit for unit in units if any(ch.isalnum() for ch in unit)]
        sentences.extend(units)

    for para in split_paragraphs(text):
        run_start: int | None = None
        run_end = 0
        after_dialogue = False
        for typ, start, end in extract_block_spans(para):
            if typ == "SPEECH":
                if run_start is not None:
                    append_run(para[run_start:run_end], touches_dialogue=True)
                    run_start = None
                after_dialogue = True
                continue
            if run_start is None:
                run_start = start
            run_end = end
        if run_start is not None:
            append_run(para[run_start:run_end], touches_dialogue=after_dialogue)
    return sentences


# ---------- out-of-character asides ----------

# Out-of-character asides are explicitly tagged ``[OOC: ...]``. Ordinary
# bracketed prose (stage directions, citations, literal dialogue) is content and
# must survive. The scanner below balances nested brackets inside an OOC aside.
_OOC_START_RE = re.compile(r"\[\s*ooc\b", re.IGNORECASE)


def strip_ooc(text: str) -> str:
    """Remove [OOC: ...] out-of-character asides, replacing each with a space
    so the words on either side don't fuse into one token.

    Used by anti_echo to drop the user's directives (and any quotes nested
    inside them) before reading their in-character dialogue.
    """
    pieces: list[str] = []
    cursor = 0
    while match := _OOC_START_RE.search(text, cursor):
        pieces.append(text[cursor : match.start()])
        depth = 0
        end = len(text)
        for i in range(match.start(), len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        pieces.append(" ")
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


# ---------- structural helpers ----------


def find_quote_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) character spans of maximal quoted regions, inclusive
    of the outer quote marks themselves. Nested quotes stay inside their outer
    span, so returned spans never overlap.

    Used by detectors that need quote positions rather than stripped narration
    (e.g. structural_repetition, anti_echo). Uses the same quote definitions as
    extract_narration so the two functions agree on what counts as dialogue.
    """
    spans: list[tuple[int, int]] = []
    stack: list[str] = []  # expected closing marks
    outer_start = 0
    for i, ch in enumerate(text):
        # Backslash-escaped straight quotes are literal. An even number of
        # preceding slashes means the quote itself is not escaped.
        if ch in TOGGLE_QUOTES:
            slashes = 0
            j = i - 1
            while j >= 0 and text[j] == "\\":
                slashes += 1
                j -= 1
            if slashes % 2:
                continue

        # U+2019 is also the normal typographic apostrophe. Between two word
        # characters it never closes curly-single dialogue.
        if ch == "’" and i > 0 and i + 1 < len(text) and text[i - 1].isalnum() and text[i + 1].isalnum():
            continue

        # A straight double quote immediately after a digit is normally an inch
        # or arc-second mark. It can still close already-open dialogue ending in
        # a number, but must not open a bogus span between two measurements.
        if ch in TOGGLE_QUOTES and not stack and i > 0 and text[i - 1].isdigit():
            continue

        if stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                spans.append((outer_start, i + 1))
            continue

        if ch in TOGGLE_QUOTES:
            if not stack:
                outer_start = i
            stack.append(ch)
        elif ch in OPEN_QUOTES:
            if not stack:
                outer_start = i
            stack.append(_QUOTE_PAIRS[ch])
        elif ch in CLOSE_QUOTES and stack:
            # A matching closer deeper in the stack recovers malformed nested
            # markup through that level. A wholly mismatched closer is literal:
            # notably, a possessive apostrophe inside double-quoted speech must
            # not close the double quote.
            if ch in stack:
                while stack:
                    expected = stack.pop()
                    if expected == ch:
                        break
            if not stack:
                spans.append((outer_start, i + 1))

    # An unmatched opener is treated as literal prose. This is the conservative
    # recovery: it cannot swallow the rest of a paragraph as dialogue, and keeps
    # find_quote_spans/extract_narration/extract_block_spans consistent.
    return spans


def count_sentences(text: str) -> int:
    """Count the sentences in a block of text.

    Non-empty text with no terminator counts as 1 (a sentence fragment or short
    imperative). Empty text returns 0.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    return len(_split_sentence_units(stripped))


# Trailing closing markers SENT_SPLIT tolerates after a terminator, plus the
# other terminal punctuation that can ride alongside a ``?`` (e.g. "?!", "??").
_TRAILING_TERMINALS = "!.…。！۔｡．।॥"


def ends_with_question(sentence: str) -> bool:
    """True when the sentence ends with a question mark, allowing for trailing
    closing markers (quotes, markdown emphasis) and adjacent terminals like "?!".

    Used by anti_echo to pick out the interrogative sentences in a draft.
    """
    trimmed = sentence.rstrip().rstrip("".join(_TRAILING_MARKERS)).rstrip(_TRAILING_TERMINALS)
    return bool(trimmed and trimmed[-1] in _QUESTION_TERMINATORS)


# ---------- block extraction (SPEECH / EMPHASIS / NARRATION) ----------
# A paragraph is decomposed into ordered spans of three block types:
#   SPEECH    — a quoted span (dialogue), markers included
#   EMPHASIS  — an *asterisk* or _underscore_ span, markers included
#   NARRATION — everything else (bare prose)
# This is the shared source of truth for structural_repetition (which only needs
# the types/counts) and format_consistency (which needs the raw offsets to splice
# a rewrite). Both go through extract_block_spans so they agree on segmentation.

EMPHASIS_RE = re.compile(
    r"(?<![\w*\\])\*(?![\s*])([^*\n]+?)\*(?![\w*])"  # *thought* (not bullet/bold/escaped)
    r"|"
    r"(?<![\w_\\])_(?![\s_])([^_\n]+?)_(?![\w_])",  # _thought_ (not __bold__)
)


def find_emphasis_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of *asterisk* / _underscore_ emphasis runs,
    inclusive of the markers. A leading ``*`` that is the first non-space on its
    line and followed by a space is treated as a markdown bullet and skipped.
    Escaped markers and Markdown ``**bold**`` / ``__bold__`` are also skipped."""
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
    emphasis_spans = [
        (s, e)
        for s, e in find_emphasis_spans(para)
        if not any(s < quote_end and quote_start < e for quote_start, quote_end in quote_spans)
    ]

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


def split_segment_sentences(text: str) -> list[str]:
    """Split text into sentences with dialogue, emphasis, and narration kept in
    separate segments — no segment ever mixes quoted speech with narration.

    Paragraph → blocks (SPEECH / EMPHASIS / NARRATION via extract_block_spans)
    → sentences within each block. So the attribution tail of a dialogue line
    ('she says, her voice dropping.') comes back as its own segment, and a
    multi-sentence quote is split inside the quote marks.

    Every returned segment is a contiguous substring of *text* (blocks tile the
    paragraph at raw offsets), which downstream substring checks — the editor's
    flagged-sentence filter and its search/replace patching — rely on.
    """
    segments: list[str] = []
    for para in split_paragraphs(text):
        for _typ, s, e in extract_block_spans(para):
            segments.extend(_split_sentence_units(para[s:e]))
    return segments


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
