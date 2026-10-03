"""Turn a mined key into the regex the Phrase Bank would store, and score that regex.

A suggestion is the bank regex itself, re-scored as that regex: a key's signal
can come from the abstraction alone (``^ P X it`` fell from 1.59 to 0.92 once
matched as a regex), so the key's statistic is recomputed on what the bank
would actually match.

Patterns must compile under both Python ``re`` and JavaScript ``new RegExp``,
because the editor validates them in the browser. They use no named groups, no
lookbehind, and no inline flags. The bank already matches case-insensitively,
one sentence at a time, and sentences keep their markup (``*A beat.*``).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from ...analysis.detectors.slop_detector import MAX_PHRASE_REGEX
from .scoring import overuse_lb, rise_z
from .shapes import FOLDED_WARDS

STRICT = "strict"
LOOSE = "loose"

_PRONOUN = r"\b(?:s?he|i|you|we|they|h(?:im|er|is)|me|us|them|my|your|our|their)"
_SLOT = {
    STRICT: r"[\w'’-]+(?:\s+[\w'’-]+){0,2}",
    LOOSE: r"[^,.;:!?—–…\"“”*]{1,40}?",
}
# A pattern-final slot is one word in both widths: a lazy slot with nothing
# after it would match a single character.
_TRAILING_SLOT = r"[\w'’-]+"
_MARK_PIECES = {
    "^": r"^[\W_]*",
    ".": r"[.!?…]*[\W_]*$",
    ",": r"\s*[,;:]\s*",
    "—": r"\s*(?:—|–|--)\s*",
    "…": r"\s*(?:\.\.\.|…)\s*",
}

_LABEL_PRONOUNS = {
    "P": "she",
    "P's": "she's",
    "P'd": "she'd",
    "P'll": "she'll",
    "P'm": "I'm",
    "P're": "they're",
    "P've": "they've",
}
_LABEL_TRIM = ' \t*_"“”~'
_FILLER_SPACE = re.compile(r"\s+")
_SPECIALIZABLE = re.compile(r"[\w'-]+(?: [\w'-]+)*")

SPECIALIZE_SHARE = 0.4
MAX_EXAMPLES = 40
TOP_FILLERS = 8


@dataclass(frozen=True, slots=True)
class Shape:
    """A key body as regex-building tokens. ``literal`` shapes hold plain words."""

    tokens: tuple[str, ...]
    literal: bool = False

    @classmethod
    def from_key(cls, key: str) -> Shape:
        body = key.split(":", 1)[1].split()
        if body[0] == "=":
            return cls(tuple(body[1:]), literal=True)
        return cls(tuple(body))

    @property
    def slot_count(self) -> int:
        return 0 if self.literal else self.tokens.count("X")

    @property
    def has_width(self) -> bool:
        """Whether strict and loose differ, i.e. some slot is not pattern-final."""
        return not self.literal and "X" in self.tokens[:-1]

    def specialize(self, slot: int, words: Sequence[str]) -> Shape:
        """This shape with its *slot*-th ``X`` written as literal *words*."""
        index = [i for i, token in enumerate(self.tokens) if token == "X"][slot]
        return Shape((*self.tokens[:index], *words, *self.tokens[index + 1 :]))


def _literal(word: str) -> str:
    text = word.replace("'", "['’]")
    return text + "s?" if word in FOLDED_WARDS else text


def build_regex(shape: Shape, width: str = STRICT, *, capture: bool = False) -> str:
    """Return the bank pattern for *shape*; a capturing copy puts each slot in a group."""
    if shape.literal:
        return r"^[\W_]*" + r"[\W_]+".join(_literal(w) for w in shape.tokens) + r"[\W_]*$"
    pieces: list[str] = []
    previous_word = False
    last = len(shape.tokens) - 1
    for index, token in enumerate(shape.tokens):
        if token in _MARK_PIECES:
            pieces.append(_MARK_PIECES[token])
            previous_word = False
            continue
        if token == "X":
            piece = _TRAILING_SLOT if index == last else _SLOT[width]
            if capture:
                piece = f"({piece})"
            if index == 0:
                # An unanchored leading slot would otherwise be retried from every
                # character; a match that starts mid-word also starts at its word.
                piece = r"\b" + piece
        elif token == "P":
            piece = _PRONOUN + r"\b"
        elif token.startswith("P'"):
            piece = _PRONOUN + "['’]" + token[2:] + r"\b"
        else:
            piece = r"\b" + _literal(token) + r"\b"
        if previous_word:
            pieces.append(r"\s+")
        pieces.append(piece)
        previous_word = True
    return "".join(pieces)


_MARK_SPELLINGS = (("—", ("—", "–", "--")), ("…", ("...", "…")), (",", (",", ";", ":")))


def prefilter_terms(shape: Shape) -> tuple[str, ...]:
    """Strings of which a folded sentence must contain one to possibly match.

    The longest literal word when there is one; otherwise the spellings of the
    rarest punctuation mark; otherwise nothing, and every sentence is tried.
    """
    words = [t for t in shape.tokens if shape.literal or (t not in _MARK_PIECES and t != "X" and not t.startswith("P"))]
    if words:
        return (max(words, key=len),)
    return next((spellings for mark, spellings in _MARK_SPELLINGS if mark in shape.tokens), ())


def shape_label(shape: Shape) -> str:
    """Readable shape: slots as ``…`` and pronouns as "she", without ``^`` or ``.``."""
    if shape.literal:
        return " ".join(shape.tokens).capitalize() + "."
    parts: list[str] = []
    for token in shape.tokens:
        if token in {"^", "."}:
            continue
        if token == "X":
            parts.append("…")
        elif token == "…":
            parts.append("...")
        else:
            parts.append(_LABEL_PRONOUNS.get(token, token))
    return " ".join(parts).replace(" ,", ",")


def fold(text: str) -> str:
    """Lowercase with curly apostrophes straightened, as prefilter terms are written."""
    return text.lower().replace("’", "'")


def _filler(text: str | None) -> str:
    return _FILLER_SPACE.sub(" ", fold(text or "")).strip()


# ── scoring a regex against the sentence corpus ─────────────────────────────


@dataclass(slots=True)
class CharacterSentences:
    """One character's reply sentences, as the bank sees them."""

    sentences: list[str] = field(default_factory=list)
    folded: list[str] = field(default_factory=list)
    recent: bytearray = field(default_factory=bytearray)

    def add(self, sentence: str, recent: bool) -> None:
        self.sentences.append(sentence)
        self.folded.append(fold(sentence))
        self.recent.append(recent)


@dataclass(slots=True)
class SentenceCorpus:
    """Reply sentences per character (characters without any are left out),
    plus every card sentence, untagged.

    The bank cannot tell narration from speech, so a regex is scored on all of
    them, still averaged per character.
    """

    characters: list[CharacterSentences]
    card: list[str]
    card_folded: list[str]
    recent_characters: int
    older_characters: int


@dataclass(slots=True)
class PatternScore:
    shape: Shape
    width: str
    pattern: str
    spread: int = 0
    recent: int = 0
    older: int = 0
    rise_z: float = 0.0
    card_observed: int = 0
    card_expected: float = 0.0
    lb: float = 0.0
    examples: list[str] = field(default_factory=list)
    fillers: list[Counter[str]] = field(default_factory=list)
    forms: Counter[str] = field(default_factory=Counter)

    def label(self) -> str:
        """The shape label; a literal key shows its commonest sentence."""
        if self.shape.literal and self.forms:
            return self.forms.most_common(1)[0][0]
        return shape_label(self.shape)

    def filler_summary(self) -> list[dict[str, object]]:
        return [{"distinct": len(c), "top": [[text, n] for text, n in c.most_common(TOP_FILLERS)]} for c in self.fillers]


def _candidates(folded: list[str], terms: tuple[str, ...]) -> list[int]:
    return [i for i, text in enumerate(folded) if not terms or any(term in text for term in terms)]


def score_shape(shape: Shape, width: str, corpus: SentenceCorpus) -> PatternScore | None:
    """Score the regex for *shape* exactly as it would be stored. ``None`` if it is too long to store.

    The generated patterns hold only words, marks, and fixed constructs, so
    length is the one bank limit they can break.
    """
    pattern = build_regex(shape, width)
    if len(pattern) > MAX_PHRASE_REGEX:
        return None
    # The capturing copy matches exactly the sentences the stored pattern does,
    # so one search per sentence serves both the count and the fillers.
    rx = re.compile(build_regex(shape, width, capture=True), re.IGNORECASE)
    terms = prefilter_terms(shape)
    score = PatternScore(shape, width, pattern, fillers=[Counter() for _ in range(shape.slot_count)])
    rate = 0.0
    for chars in corpus.characters:
        hits = 0
        recent = older = False
        for i in _candidates(chars.folded, terms):
            sentence = chars.sentences[i]
            match = rx.search(sentence)
            if match is None:
                continue
            if hits == 0 and len(score.examples) < MAX_EXAMPLES:
                score.examples.append(sentence.strip())
            hits += 1
            if chars.recent[i]:
                recent = True
            else:
                older = True
            if shape.literal:
                score.forms[sentence.strip(_LABEL_TRIM)] += 1
            for group, fillers in enumerate(score.fillers, 1):
                fillers[_filler(match.group(group))] += 1
        if hits:
            score.spread += 1
            score.recent += recent
            score.older += older
            rate += hits / len(chars.sentences)
    card_hits = sum(1 for i in _candidates(corpus.card_folded, terms) if rx.search(corpus.card[i]))
    score.card_observed = card_hits
    characters = len(corpus.characters)
    score.card_expected = rate / characters * len(corpus.card) if characters else 0.0
    score.lb = overuse_lb(score.card_expected, card_hits)
    score.rise_z = rise_z(score.recent, corpus.recent_characters, score.older, corpus.older_characters)
    return score


def best_pattern(shape: Shape, corpus: SentenceCorpus) -> PatternScore | None:
    """Score both slot widths, keep the higher ``lb``, then try specializing slots.

    A slot whose commonest filler covers 40% of its matches is also tried as
    that literal; the specialized pattern wins when its ``lb`` is at least as
    high (``X enough that`` becomes ``close enough that``).
    """
    widths = (STRICT, LOOSE) if shape.has_width else (STRICT,)
    scored = [s for s in (score_shape(shape, width, corpus) for width in widths) if s is not None]
    if not scored:
        return None
    best = max(scored, key=lambda s: s.lb)
    slot = 0
    while slot < best.shape.slot_count:
        counts = best.fillers[slot]
        total = sum(counts.values())
        if total:
            filler, count = counts.most_common(1)[0]
            if count / total >= SPECIALIZE_SHARE and _SPECIALIZABLE.fullmatch(filler):
                special = score_shape(best.shape.specialize(slot, filler.split()), best.width, corpus)
                if special is not None and special.lb >= best.lb:
                    best = special
                    continue  # the next slot now has this index
        slot += 1
    return best
