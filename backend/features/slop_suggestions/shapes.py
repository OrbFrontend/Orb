"""Build narration (n) and speech (d) sentence keys.

Skeletons retain function words/punctuation, replace pronouns with P and content-word runs with X. Yield skeleton n-grams and
literal keys for sentences of at most six words; tags keep speech/narration corpora separate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from ...analysis.text.lexical import STOPWORDS
from ...analysis.text.roleplay_segmentation import extract_block_spans, split_paragraphs

NARRATION = "n"
SPEECH = "d"

MIN_SHAPE_TOKENS = 3
MAX_SHAPE_TOKENS = 8
LITERAL_MAX_WORDS = 6

#: Stands in for ``{{char}}`` and ``{{user}}`` in card text. It is in the name
#: set, so a card's ``{{char}} nods.`` never becomes a literal key.
PLACEHOLDER_NAME = "Avery"

TOKEN_RE = re.compile(r"\.\.\.|…|—|–|--|[^\W_]+(?:'[^\W_]+)*|[,;:.!?\n]")
WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*")

PRONOUNS = frozenset(
    "i you he she we they me him her us them my your his our their mine yours hers ours theirs "
    "myself yourself yourselves himself herself ourselves themselves".split()
)
_PRONOUN_CONTRACTION = re.compile(r"(?:i|you|he|she|we|they)('m|'re|'s|'d|'ll|'ve)")

_SENTENCE_END = frozenset({".", "!", "?", "\n"})
_PUNCTUATION = {"...": "…", "…": "…", "—": "—", "–": "—", "--": "—", ",": ",", ";": ",", ":": ","}

#: Skeleton tokens that are not words. ``P`` counts as a word.
MARKS = frozenset({"X", ",", ".", "…", "—", "^"})

#: Card authors write "towards" 73% of the time and models "toward" 69% of the
#: time; combined usage is at parity. Folding keeps a spelling dialect from
#: ranking as slop. Derived, so a new ``-wards`` stopword folds too.
FOLDED_WARDS = frozenset(word[:-1] for word in STOPWORDS if word.endswith("wards"))


@dataclass(frozen=True, slots=True)
class Sentence:
    """One abstracted sentence: its block tag, skeleton, and lowercased words."""

    tag: str
    tokens: tuple[str, ...]
    words: tuple[str, ...]


def abstract(block: str, tag: str) -> list[Sentence]:
    """Split one block into abstracted sentences. Sentences without a word are dropped."""
    out: list[Sentence] = []
    tokens: list[str] = ["^"]
    words: list[str] = []

    def close() -> None:
        nonlocal tokens, words
        if words:
            out.append(Sentence(tag, (*tokens, "."), tuple(words)))
        tokens, words = ["^"], []

    for raw in TOKEN_RE.findall(block.replace("’", "'")):
        low = raw.lower()
        if low in _SENTENCE_END:
            close()
            continue
        mark = _PUNCTUATION.get(low)
        if mark is not None:
            tokens.append(mark)
            continue
        if low.endswith("wards") and low in STOPWORDS:
            low = low[:-1]
        words.append(low)
        if low in PRONOUNS:
            token = "P"
        elif contraction := _PRONOUN_CONTRACTION.fullmatch(low):
            token = "P" + contraction.group(1)
        elif low in STOPWORDS:
            token = low
        else:
            token = "X"
        if token == "X" and tokens[-1] == "X":
            continue
        tokens.append(token)
    close()
    return out


def sentences(text: str) -> Iterator[Sentence]:
    """Yield every abstracted sentence of *text*, block by block."""
    for paragraph in split_paragraphs(text):
        for typ, start, end in extract_block_spans(paragraph):
            yield from abstract(paragraph[start:end], SPEECH if typ == "SPEECH" else NARRATION)


def shape_bodies(tokens: Sequence[str]) -> list[str]:
    """The shape keys of one skeleton, without their tag.

    A key is a 3-8 token n-gram holding at least one ``X``. It starts with neither ``,`` nor ``.``, does not end with ``,``, and
    has two words or spans the whole sentence with one. Prefix counts keep this linear in the number of grams; it runs for every
    sentence of every reply, twice.
    """
    length = len(tokens)
    slots = [0]
    words = [0]
    for token in tokens:
        slots.append(slots[-1] + (token == "X"))
        words.append(words[-1] + (token not in MARKS))
    out: list[str] = []
    for start in range(length - MIN_SHAPE_TOKENS + 1):
        if tokens[start] in {",", "."}:
            continue
        for end in range(start + MIN_SHAPE_TOKENS, min(start + MAX_SHAPE_TOKENS, length) + 1):
            if slots[end] == slots[start] or tokens[end - 1] == ",":
                continue
            count = words[end] - words[start]
            if count >= 2 or (count >= 1 and start == 0 and end == length):
                out.append(" ".join(tokens[start:end]))
    return out


def sentence_keys(sentence: Sentence, names: frozenset[str]) -> set[str]:
    """Every tagged key of one sentence, with a literal key (``= a beat``) if it is short and names no one."""
    keys = {f"{sentence.tag}:{body}" for body in shape_bodies(sentence.tokens)}
    if len(sentence.words) <= LITERAL_MAX_WORDS and names.isdisjoint(sentence.words):
        keys.add(f"{sentence.tag}:= " + " ".join(sentence.words))
    return keys


def key_core(key: str) -> tuple[str, ...]:
    """The key's tokens without its tag, ``^``, ``=``, or trailing ``.``."""
    tokens = key.split(":", 1)[1].split()
    if tokens and tokens[0] in {"^", "="}:
        tokens = tokens[1:]
    if tokens and tokens[-1] == ".":
        tokens = tokens[:-1]
    return tuple(tokens)


# ── card-authored baseline ──────────────────────────────────────────────────

_START_RE = re.compile(r"<START>", re.IGNORECASE)
_EXAMPLE_LABEL_RE = re.compile(r"^[ \t]*(?:(?i:\{\{(?:char|user)\}\})|[A-Z][\w .'’-]{0,30}?)[ \t]*:[ \t]*", re.MULTILINE)
_MACRO_RE = re.compile(r"\{\{(?:char|user)\}\}", re.IGNORECASE)


def card_baseline_text(first_mes: str, alternate_greetings: Iterable[str], mes_example: str) -> str:
    """A card's authored roleplay prose: greetings plus cleaned dialogue examples.

    ``mes_example`` loses its ``<START>`` separators and leading ``Name:`` labels, and macros become a placeholder name. Each
    part is its own paragraph so no sentence runs across two of them.
    """
    example = _EXAMPLE_LABEL_RE.sub("", _START_RE.sub("\n\n", mes_example or ""))
    parts = [first_mes or "", *(greeting for greeting in alternate_greetings if isinstance(greeting, str)), example]
    return _MACRO_RE.sub(PLACEHOLDER_NAME, "\n\n".join(part for part in parts if part.strip()))


# ── names ───────────────────────────────────────────────────────────────────


def build_names(names: Iterable[str], descriptions: Iterable[str], card_texts: Iterable[str]) -> frozenset[str]:
    """Lowercased name tokens that keep a short sentence out of the literal keys.

    Explicit *names* (characters, personas, cards, group members) are joined by tokens capitalized mid-sentence in card
    *descriptions* that never appear lowercase in the descriptions or *card_texts*. Stopwords and tokens of two letters or fewer
    are dropped: "Oscar" belongs here, "He" and "Al" do not.
    """
    found = {word.lower() for name in names for word in WORD_RE.findall(name.replace("’", "'"))}
    found.add(PLACEHOLDER_NAME.lower())
    capitalized: set[str] = set()
    lowercase: set[str] = set()
    for text, is_description in [*((d, True) for d in descriptions), *((t, False) for t in card_texts)]:
        sentence_start = True
        for raw in TOKEN_RE.findall((text or "").replace("’", "'")):
            if raw in _SENTENCE_END:
                sentence_start = True
                continue
            if raw in _PUNCTUATION:
                continue
            if raw == raw.lower():
                lowercase.add(raw)
            elif is_description and not sentence_start and raw[0].isupper():
                capitalized.add(raw.lower())
            sentence_start = False
    found |= capitalized - lowercase
    return frozenset(word for word in found if len(word) > 2 and word not in STOPWORDS)
