"""Flag narration that repeatedly describes what does not happen.

The detector reads one draft, never its history. It excludes dialogue,
standalone thoughts, and protected regions (fences, HTML, OOC asides) by
source offset, so every finding addresses its exact characters in the input.
A message passes the gate only with at least ``min_hits`` raw shape matches;
a single denial is often good writing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...core.text_segmentation import (
    PARA_SPLIT,
    ends_with_question,
    find_quote_spans,
    split_sentence_units,
)
from ..text.lexical import TOKEN_RE, normalize_word
from ..text.roleplay import THOUGHT_ATTRIBUTION, is_inline_emphasis
from ..text.roleplay_segmentation import find_emphasis_spans, ooc_spans

__all__ = [
    "NegationConstituent",
    "NegationFinding",
    "NegationResult",
    "detect_negated_narration",
    "evaluate_negated_narration",
]

ASTERISK = "asterisk"
PROSE = "prose"
_STYLES = (ASTERISK, PROSE)


@dataclass(slots=True)
class NegationConstituent:
    """One raw shape, or the absorbed pivot, inside a chained finding."""

    kind: str
    start: int
    end: int
    sentence_count: int


@dataclass(slots=True)
class NegationFinding:
    kinds: list[str]  # constituent kinds in order; optional "pivot" only last
    start: int  # inclusive Python str character offset in source_text
    end: int  # exclusive character offset; not a UTF-8 byte offset
    span: str  # source_text[start:end], with original whitespace/markers
    sentences: list[str]  # source-contiguous constituent sentence slices
    denial_count: int  # matched denial sentences; excludes affirmative payoff
    pivot_span: str | None
    constituents: list[NegationConstituent] = field(default_factory=list)


@dataclass(slots=True)
class NegationResult:
    source_text: str  # exact input for offset validation; not serialized
    findings: list[NegationFinding]  # chained; empty below the gate
    raw_hits: int  # shape matches before chaining or pivot absorption
    narration_sentences: int
    negated_sentences: int  # usable units with recognized head negation
    style: str = PROSE  # narration interpretation the runs were built with
    raw_shapes: dict[str, int] = field(default_factory=dict)  # raw_hits by kind

    @property
    def density(self) -> float:
        return self.negated_sentences / self.narration_sentences if self.narration_sentences else 0.0


# ── Vocabulary ────────────────────────────────────────────────────────────────

_NEG_WORDS = frozenset("not never no nobody nothing none neither nor nowhere cannot".split())
_COPULA_NT = frozenset("isn't aren't wasn't weren't ain't".split())
_BE = frozenset("is am are was were be been being".split())
_BE_CONTRACTED = frozenset("it's that's he's she's there's i'm we're they're you're".split())
_AUX = frozenset("do does did can could will would shall should has have had must may might need".split())
_RELATIVE = frozenset("that who which whose whom where".split())
_WH = frozenset("how what where why when who whether about".split())
_PIVOTS = frozenset("just only instead rather simply".split())
_PRONOUNS = frozenset("i you he she it we they".split())
_DETERMINERS = frozenset("the a an this that these those his her their its my your our".split())
_CONNECTIVES = frozenset("but so because though".split())
_HEDGES = frozenset("yet quite really exactly entirely now always anymore once".split())
_SAY_VERBS = frozenset(
    "say says said saying tell tells told answer answers answered reply replies replied "
    "mouth mouths mouthed shout shouts shouted whisper whispers whispered mutter mutters muttered".split()
)
# Words that make a would-be descriptive fragment read as a clause.
_FINITE_CUES = _BE | _AUX | _BE_CONTRACTED
_FRAGMENT_BLOCKERS = _PRONOUNS | frozenset("this that these those there then and but so".split())

_EXPANSIONS = {
    "isn't": ("is", "not"),
    "wasn't": ("was", "not"),
    "aren't": ("are", "not"),
    "weren't": ("were", "not"),
    "ain't": ("is", "not"),
}

_OUTER = '*_"“”‘’'
_CLAUSE_PUNCT = re.compile(r"[,;:—–]")
_CLAUSE_SPLIT = re.compile(r"[,;:—–]|\s+(?:and|or|nor)\s+", re.IGNORECASE)
_HEDGE_TAIL = re.compile(r"not\s+(?:" + "|".join(sorted(_HEDGES)) + r")\W*", re.IGNORECASE)
_MARKUP = re.compile(r"<[^>]*>|<!--|-->|\{\{|\}\}|https?://|www\.|\w\(", re.IGNORECASE)
_TAG_AUX = r"(?:do|does|did|is|are|was|were|will|would|can|could|has|have|had|should)"
_TAG_SUBJECT = r"(?:i|you|he|she|it|we|they|there)"
# A negative tag anywhere (", didn't he"); an affirmative one only at the end
# (", did she."), so ", did she go" mid-sentence is not mistaken for a tag.
_TAG_QUESTION = re.compile(
    rf",\s*(?:(?:{_TAG_AUX}|wo|ca)n't\s+{_TAG_SUBJECT}\b|{_TAG_AUX}\s+{_TAG_SUBJECT}\W*$)",
    re.IGNORECASE,
)
_INTERJECTION = re.compile(
    r"(?:(?:oh|ah),?\s+)?no+(?:\s*[,.!…—–-]+\s*(?:(?:oh|ah),?\s+)?(?:no+|oh|god|wait))*[\s.!…—–-]*",
    re.IGNORECASE,
)
_NO_FOLLOWER_PUNCT = frozenset(",;:—–.!?…")


def _is_negator(word: str) -> bool:
    return word in _NEG_WORDS or word.endswith("n't")


def _strip_outer(text: str) -> str:
    return text.strip().strip(_OUTER).strip()


# ── Excluded regions ──────────────────────────────────────────────────────────

_FENCE = re.compile(r"```")
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_HTML_TAG = re.compile(r"<(/?)([A-Za-z][\w:-]*)\b[^<>]*?(/?)>")
_VOID_TAGS = frozenset("br hr img input meta link wbr source area col embed param track".split())
_DIVIDER = re.compile(r"^[ \t]*(?:[*_-][ \t]*){3,}$", re.MULTILINE)


def _fence_regions(text: str) -> list[tuple[int, int]]:
    marks = [m.start() for m in _FENCE.finditer(text)]
    regions = [(marks[k], marks[k + 1] + 3) for k in range(0, len(marks) - 1, 2)]
    if len(marks) % 2:
        regions.append((marks[-1], len(text)))  # an unclosed fence excludes its tail
    return regions


def _html_regions(text: str) -> list[tuple[int, int]]:
    regions = [m.span() for m in _HTML_COMMENT.finditer(text)]
    stack: list[tuple[str, int, int]] = []
    for match in _HTML_TAG.finditer(text):
        closing, name, self_closing = match.group(1), match.group(2).lower(), match.group(3)
        if closing:
            names = [entry[0] for entry in stack]
            if name not in names:
                regions.append(match.span())
                continue
            while stack:
                open_name, open_start, _ = stack.pop()
                if open_name == name:
                    regions.append((open_start, match.end()))
                    break
        elif self_closing or name in _VOID_TAGS:
            regions.append(match.span())
        else:
            stack.append((name, match.start(), match.end()))
    # Unmatched opening tags exclude only themselves.
    regions.extend((start, end) for _, start, end in stack)
    return regions


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        elif end > start:
            merged.append((start, end))
    return merged


def _excluded_regions(text: str) -> list[tuple[int, int]]:
    """Protected ranges, found on the unmodified source."""
    fences = _fence_regions(text)

    def outside_fences(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [(s, e) for s, e in spans if not any(fs <= s < fe for fs, fe in fences)]

    return _merge(
        fences
        + outside_fences(_html_regions(text))
        + outside_fences(ooc_spans(text))
        + outside_fences([m.span() for m in _DIVIDER.finditer(text)])
    )


# ── Paragraph tiling ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Paragraph:
    start: int  # absolute offset of text[start:end]
    text: str
    tiles: list[tuple[str, int, int]]  # paragraph-relative SPEECH/EMPHASIS/NARRATION


_DOUBLE_OPEN = frozenset('"＂“')


def _stray_quote_regions(para: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Conservatively exclude around quote marks the balanced parse left unpaired."""
    regions: list[tuple[int, int]] = []
    for i, ch in enumerate(para):
        if ch not in _DOUBLE_OPEN and ch != "”":
            continue
        if any(s <= i < e for s, e in spans):
            continue
        if ch in '"＂' and i > 0 and (para[i - 1].isdigit() or para[i - 1] == "\\"):
            continue
        # An unclosed opening quote runs to the paragraph end (multi-paragraph
        # speech); a close without an opener continues speech from its start.
        regions.append((0, i + 1) if ch == "”" else (i, len(para)))
    return regions


def _opens_like(text: str, i: int) -> bool:
    before = text[i - 1] if i else " "
    after = text[i + 1] if i + 1 < len(text) else " "
    return (before.isspace() or before in "([{—–-") and not after.isspace()


def _closes_like(text: str, i: int) -> bool:
    before = text[i - 1] if i else " "
    after = text[i + 1] if i + 1 < len(text) else " "
    return not before.isspace() and (after.isspace() or after in ".,;:!?…—–-)*_")


def _quotes_well_formed(segment: str, spans: list[tuple[int, int]]) -> bool:
    """Whether the parity pairing of *segment*'s quotes can be trusted across paragraphs."""
    if _stray_quote_regions(segment, spans):
        return False
    for start, end in spans:
        close = end - 1
        if segment[start] in '"＂' and _closes_like(segment, start) and not _opens_like(segment, start):
            return False
        if segment[close] in '"＂' and _opens_like(segment, close) and not _closes_like(segment, close):
            return False
    return True


def _segments(text: str, regions: list[tuple[int, int]]) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    cursor = 0
    for start, end in regions:
        if start > cursor:
            segments.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < len(text):
        segments.append((cursor, len(text)))
    return segments


def _paragraph_ranges(segment: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for match in [*PARA_SPLIT.finditer(segment), None]:
        end = match.start() if match else len(segment)
        piece = segment[cursor:end]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            trail = len(piece) - len(piece.rstrip())
            ranges.append((cursor + lead, end - trail))
        if match:
            cursor = match.end()
    return ranges


def _tile(para: str, speech: list[tuple[int, int]]) -> list[tuple[str, int, int]]:
    emphasis = [(s, e) for s, e in find_emphasis_spans(para) if not any(s < qe and qs < e for qs, qe in speech)]
    typed = sorted([(s, e, "SPEECH") for s, e in speech] + [(s, e, "EMPHASIS") for s, e in emphasis])
    tiles: list[tuple[str, int, int]] = []
    cursor = 0
    for start, end, typ in typed:
        if start < cursor:
            continue
        if cursor < start:
            tiles.append(("NARRATION", cursor, start))
        tiles.append((typ, start, end))
        cursor = end
    if cursor < len(para):
        tiles.append(("NARRATION", cursor, len(para)))
    return tiles


def _paragraphs(text: str) -> list[_Paragraph]:
    paragraphs: list[_Paragraph] = []
    for seg_start, seg_end in _segments(text, _excluded_regions(text)):
        segment = text[seg_start:seg_end]
        # A balanced quote may span paragraphs. When a quote mark is left
        # unpaired, per-paragraph parsing adds what the malformed quote would
        # otherwise flip from speech to narration; the union only grows speech.
        segment_quotes = find_quote_spans(segment)
        balanced = _quotes_well_formed(segment, segment_quotes)
        for p_start, p_end in _paragraph_ranges(segment):
            para = segment[p_start:p_end]
            speech = [
                (max(qs, p_start) - p_start, min(qe, p_end) - p_start)
                for qs, qe in segment_quotes
                if qs < p_end and qe > p_start
            ]
            if not balanced:
                local = find_quote_spans(para)
                speech += local + _stray_quote_regions(para, local)
            paragraphs.append(_Paragraph(seg_start + p_start, para, _tile(para, _merge(speech))))
    return paragraphs


def _classify_style(paragraphs: list[_Paragraph]) -> str:
    """Asterisk style when emphasis covers >30% of analyzable text and exceeds plain narration."""
    emphasis = narration = total = 0
    for para in paragraphs:
        for typ, start, end in para.tiles:
            total += end - start
            if typ == "EMPHASIS":
                emphasis += end - start
            elif typ == "NARRATION":
                narration += end - start
    return ASTERISK if emphasis > 0.30 * total and emphasis > narration else PROSE


# ── Narration runs ────────────────────────────────────────────────────────────

_TERMINAL = ".!?…"


def _starts_mid_sentence(tiles: list[tuple[str, int, int]], i: int, para: str) -> bool:
    """Sentence-initial emphasis whose sentence continues in plain narration (``*Nothing* moves.``)."""
    _, start, end = tiles[i]
    if para[start + 1 : end - 1].rstrip()[-1:] in tuple(_TERMINAL):
        return False
    if i + 1 >= len(tiles) or tiles[i + 1][0] != "NARRATION":
        return False
    following = para[tiles[i + 1][1] : tiles[i + 1][2]].lstrip()
    return bool(following) and (following[0].islower() or following[0] in ",;:—–")


def _prose_run_ranges(para: _Paragraph) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for i, (typ, start, end) in enumerate(para.tiles):
        joins = typ == "NARRATION" or (
            typ == "EMPHASIS"
            and (
                is_inline_emphasis(para.tiles, i, para.text)
                or (_starts_mid_sentence(para.tiles, i, para.text) and not THOUGHT_ATTRIBUTION.match(para.text[end:]))
            )
        )
        if joins:
            current = (current[0] if current else start, end)
        elif current:
            ranges.append(current)
            current = None
    if current:
        ranges.append(current)
    return ranges


def _asterisk_run_ranges(para: _Paragraph) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for typ, start, end in para.tiles:
        if typ == "EMPHASIS":
            current = (current[0] if current else start, end)
        elif typ == "NARRATION" and not para.text[start:end].strip():
            continue  # whitespace between narration blocks does not end a run
        elif current:
            ranges.append(current)  # plain text is speech in this style
            current = None
    if current:
        ranges.append(current)
    return ranges


@dataclass(slots=True)
class _Unit:
    start: int
    end: int
    text: str
    words: list[str]
    word_spans: list[tuple[int, int]]
    negs: list[int]  # indices of genuine negations

    @property
    def head(self) -> bool:
        return bool(self.negs) and self.negs[0] < 5

    @property
    def affirmative(self) -> bool:
        return not self.negs


def _genuine_negations(text: str, words: list[str], spans: list[tuple[int, int]]) -> list[int]:
    found: list[int] = []
    for i, word in enumerate(words):
        if not _is_negator(word):
            continue
        following = words[i + 1] if i + 1 < len(words) else None
        if word == "not" and following == "only":
            continue  # additive "not only ... but"
        if word == "no":
            previous = words[i - 1] if i else None
            gap = text[spans[i][1] : spans[i + 1][0]] if following is not None else ""
            if previous in ("yes", "or") or following == "or" or previous in _SAY_VERBS:
                continue  # a value: "yes or no", "say no"
            if following is None or any(ch in _NO_FOLLOWER_PUNCT for ch in gap):
                continue  # "the answer was no", interjection "No, ..."
        found.append(i)
    return found


def _usable(text: str, words: list[str]) -> bool:
    folded = text.replace("’", "'")
    if not words or _MARKUP.search(folded) or ends_with_question(text) or _TAG_QUESTION.search(folded):
        return False
    lead = words[1:] if words[0] == "but" else words
    if lead[:1] == ["don't"] or lead[:2] == ["do", "not"]:
        return False  # imperative
    return not _INTERJECTION.fullmatch(_strip_outer(text))


def _make_unit(start: int, unit: str) -> _Unit:
    matches = [(normalize_word(m.group(0)), m.span()) for m in TOKEN_RE.finditer(unit)]
    matches = [(w, span) for w, span in matches if w]
    words = [w for w, _ in matches]
    spans = [span for _, span in matches]
    return _Unit(start, start + len(unit), unit, words, spans, _genuine_negations(unit, words, spans))


def _runs(text: str, paragraphs: list[_Paragraph], style: str) -> tuple[list[list[_Unit]], int]:
    """Usable narration units grouped into runs no shape may cross."""
    runs: list[list[_Unit]] = []
    usable = 0
    for para in paragraphs:
        ranges = _asterisk_run_ranges(para) if style == ASTERISK else _prose_run_ranges(para)
        for rel_start, rel_end in ranges:
            base = para.start + rel_start
            run_text = text[base : para.start + rel_end]
            run: list[_Unit] = []
            cursor = 0
            for unit in split_sentence_units(run_text):
                pos = run_text.index(unit, cursor)
                cursor = pos + len(unit)
                if not any(ch.isalnum() for ch in unit):
                    continue  # punctuation left beside dialogue
                candidate = _make_unit(base + pos, unit)
                if not _usable(unit, candidate.words):
                    if run:
                        runs.append(run)
                    run = []
                    continue
                usable += 1
                run.append(candidate)
            if run:
                runs.append(run)
    return runs, usable


# ── Shapes ────────────────────────────────────────────────────────────────────


def _without_but(words: list[str]) -> list[str]:
    return words[1:] if words[:1] in (["but"], ["and"]) else words


def _subject(unit: _Unit) -> tuple[str, ...]:
    """Words before the first negation, without a leading conjunction."""
    offset = len(unit.words) - len(_without_but(unit.words))
    if not unit.negs or unit.negs[0] <= offset:
        return ()
    return tuple(unit.words[offset : unit.negs[0]][:4])


def _subject_change(subject: tuple[str, ...], lead: str | None) -> bool:
    return lead in _PRONOUNS and len(subject) == 1 and subject[0] in _PRONOUNS and subject[0] != lead


def _expand(words: list[str]) -> list[str]:
    out: list[str] = []
    for word in words:
        if word in _EXPANSIONS:
            out.extend(_EXPANSIONS[word])
        elif word in _BE_CONTRACTED:
            head, _, tail = word.partition("'")
            out.extend((head, "are" if tail == "re" else "am" if tail == "m" else "is"))
        else:
            out.append(word)
    return out


def _copula_subject(words: list[str], *, negated: bool) -> tuple[str, ...] | None:
    expanded = _expand(_without_but(words))
    for k in range(1, min(len(expanded), 5)):
        if expanded[k] in _BE:
            is_negated = k + 1 < len(expanded) and expanded[k + 1] == "not"
            return tuple(expanded[:k]) if is_negated == negated else None
    return None


def _same_referent(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    if first == second:
        return True
    if second != ("it",):
        return False
    # A thing named by a determiner phrase or a demonstrative may become "it";
    # a personal pronoun never does.
    return first in (("this",), ("that",)) or (len(first) >= 2 and first[0] in _DETERMINERS)


def _descriptive_fragment(unit: _Unit) -> bool:
    words = unit.words
    if len(words) > 6 or words[0] in _FRAGMENT_BLOCKERS or any(w in _FINITE_CUES for w in words):
        return False
    return not any(len(w) > 3 and (w.endswith("ed") or (w.endswith("s") and not w.endswith("ss"))) for w in words[1:])


def _split_contrast(first: _Unit, second: _Unit) -> bool:
    if not (first.head and len(first.words) <= 12 and second.affirmative and 1 <= len(second.words) <= 10):
        return False
    # 1. "Not loud. Conversational."
    if first.words[0] == "not" and first.negs[0] == 0:
        if (len(first.words) < 2 or first.words[1] not in _HEDGES) and _descriptive_fragment(second):
            return True
    # 2. "The question isn't curious. It's accusing."
    denied = _copula_subject(first.words, negated=True)
    if denied:
        affirmed = _copula_subject(second.words, negated=False)
        if affirmed and _same_referent(denied, affirmed):
            return True
    # 3. "She doesn't say anything else. Just waits."
    return _explicit_pivot(second, _subject(first))


def _explicit_pivot(unit: _Unit, subject: tuple[str, ...]) -> bool:
    words = unit.words
    if words[0] in _PIVOTS:
        return not _subject_change(subject, words[1] if len(words) > 1 else None)
    if subject and tuple(words[: len(subject)]) == subject and len(words) > len(subject):
        return words[len(subject)] in _PIVOTS
    # A named or determiner subject may continue as a third-person pronoun.
    named = bool(subject) and not (len(subject) == 1 and subject[0] in _PRONOUNS)
    return named and len(words) > 1 and words[0] in ("he", "she", "they", "it") and words[1] in _PIVOTS


def _stacked(unit: _Unit) -> bool:
    text = unit.text
    bounds = [0]
    for match in _CLAUSE_SPLIT.finditer(text):
        bounds.extend((match.start(), match.end()))
    bounds.append(len(text))
    clauses: list[int] = []  # index of each clause's first word
    for k in range(0, len(bounds), 2):
        start, end = bounds[k], bounds[k + 1]
        first = next((i for i, (s, _) in enumerate(unit.word_spans) if start <= s < end), None)
        if first is None:
            continue
        if clauses and _HEDGE_TAIL.fullmatch(_strip_outer(text[start:end])):
            continue  # "—not yet." is a hedge, not a parallel denial
        clauses.append(first)
    if len(clauses) < 2:
        return False
    negs = set(unit.negs)
    later = sum(first in negs for first in clauses[1:])
    first_denied = any(clauses[0] <= i < clauses[0] + 3 and (len(clauses) < 2 or i < clauses[1]) for i in negs)
    return later >= 2 or (later >= 1 and first_denied)


def _negative_subject_length(words: list[str]) -> int:
    if words[:1] in (["nobody"], ["nothing"]):
        size = 1
    elif words[:2] == ["no", "one"]:
        size = 2
    elif words[:3] in (["neither", "of", "them"], ["none", "of", "them"]):
        size = 3
    else:
        return 0
    return size + 1 if words[size : size + 1] == ["else"] else size


def _null_reaction(unit: _Unit) -> bool:
    words = unit.words
    if not (2 <= len(words) <= 9) or _CLAUSE_PUNCT.search(unit.text) or not unit.head:
        return False
    body = _without_but(words)
    offset = len(words) - len(body)
    if set(" ".join(body).replace("so much as", " ").split()) & _CONNECTIVES:
        return False
    subject_len = _negative_subject_length(body)
    if subject_len and offset in unit.negs:
        return len(body) > subject_len and body[subject_len] not in _BE
    k = unit.negs[0] - offset
    if k <= 0:
        return False  # needs a subject before the negator
    before = body[:k]
    if set(before[1:] if before[0] == "that" else before) & _RELATIVE:
        return False
    negator = body[k]
    if negator in _COPULA_NT or (negator == "not" and (before[-1] in _BE or before[-1] in _BE_CONTRACTED)):
        complement = body[k + 1] if k + 1 < len(body) else ""
        if len(complement) > 4 and complement.endswith("ing"):
            return True
        subject = [w.partition("'")[0] for w in before]
        return subject in (["it"], ["this"], ["that"]) and bool(complement) and complement not in _WH
    return negator in ("never", "cannot") or negator.endswith("n't") or (negator == "not" and before[-1] in _AUX)


def _match_shapes(run: list[_Unit]) -> list[tuple[str, int, int]]:
    matches: list[tuple[str, int, int]] = []
    i = 0
    while i < len(run):
        if run[i].head:
            j = i
            while j < len(run) and run[j].head:
                j += 1
            if j - i >= 2:
                matches.append(("cascade", i, j))
                i = j
                continue
        if i + 1 < len(run) and _split_contrast(run[i], run[i + 1]):
            matches.append(("split_contrast", i, i + 2))
            i += 2
        elif _stacked(run[i]):
            matches.append(("stacked", i, i + 1))
            i += 1
        elif _null_reaction(run[i]):
            matches.append(("null_reaction", i, i + 1))
            i += 1
        else:
            i += 1
    return matches


def _is_pivot(unit: _Unit, subject: tuple[str, ...]) -> bool:
    words = unit.words
    if not unit.affirmative or not 1 <= len(words) <= 14:
        return False
    if words[0] in _PIVOTS:
        lead = words[1] if len(words) > 1 else None
    elif len(words) > 1 and words[1] in _PIVOTS:
        lead = words[0]
    else:
        return False
    return not _subject_change(subject, lead)


def _chain(text: str, run: list[_Unit], matches: list[tuple[str, int, int]]) -> list[NegationFinding]:
    groups: list[list[tuple[str, int, int]]] = []
    for match in matches:
        if groups and groups[-1][-1][2] == match[1]:
            groups[-1].append(match)
        else:
            groups.append([match])

    findings: list[NegationFinding] = []
    for group in groups:
        first, last = group[0][1], group[-1][2]
        constituents = [NegationConstituent(kind, run[i].start, run[j - 1].end, j - i) for kind, i, j in group]
        denials = sum(j - i if kind == "cascade" else 1 for kind, i, j in group)
        pivot_span: str | None = None
        if group[-1][0] != "split_contrast" and last < len(run):
            subject = next((s for s in (_subject(run[k]) for k in range(first, last)) if s), ())
            if _is_pivot(run[last], subject):
                pivot = run[last]
                constituents.append(NegationConstituent("pivot", pivot.start, pivot.end, 1))
                pivot_span = pivot.text
                last += 1
        start, end = run[first].start, run[last - 1].end
        findings.append(
            NegationFinding(
                kinds=[c.kind for c in constituents],
                start=start,
                end=end,
                span=text[start:end],
                sentences=[u.text for u in run[first:last]],
                denial_count=denials,
                pivot_span=pivot_span,
                constituents=constituents,
            )
        )
    return findings


def _detect(text: str, *, min_hits: int, style: str | None) -> NegationResult:
    if min_hits < 0:
        raise ValueError("min_hits must be zero or positive")
    if style is not None and style not in _STYLES:
        raise ValueError(f"style must be one of {_STYLES}")
    paragraphs = _paragraphs(text)
    resolved = style or _classify_style(paragraphs)
    runs, usable = _runs(text, paragraphs, resolved)

    findings: list[NegationFinding] = []
    raw_shapes: dict[str, int] = {}
    for run in runs:
        matches = _match_shapes(run)
        for kind, _, _ in matches:
            raw_shapes[kind] = raw_shapes.get(kind, 0) + 1
        findings.extend(_chain(text, run, matches))

    raw_hits = sum(raw_shapes.values())
    return NegationResult(
        source_text=text,
        findings=findings if raw_hits >= min_hits else [],
        raw_hits=raw_hits,
        narration_sentences=usable,
        negated_sentences=sum(u.head for run in runs for u in run),
        style=resolved,
        raw_shapes=raw_shapes,
    )


def detect_negated_narration(text: str, *, min_hits: int = 2) -> NegationResult:
    """Detect repeated narration of what does not happen in one draft.

    ``min_hits=0`` disables only the message gate, for fixtures and evaluation.
    """
    return _detect(text, min_hits=min_hits, style=None)


def evaluate_negated_narration(text: str, style: str, *, min_hits: int = 0) -> NegationResult:
    """Re-detect a full patched draft under an explicit narration style.

    Evaluation only, not an application setting: a repair benchmark scores the
    edited draft with the original draft's interpretation instead of letting a
    shortened draft re-infer its style. Every exclusion rule still applies.
    """
    return _detect(text, min_hits=min_hits, style=style)
