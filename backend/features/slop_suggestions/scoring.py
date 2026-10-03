"""Count keys across characters and rank them against card-authored text.

Each character counts once, so one long chat cannot carry a key. A key's reply
rate is each character's share of sentences containing it, averaged over every
character; multiplied by the card corpus's sentence count it says how often card
authors would write the key if they wrote like the model. ``lb`` divides that
expectation by a 95% upper bound on what card text actually shows, so an absence
from card text counts only once the expected count is large.

Pure: every function works on plain iterables and never touches the database.
"""

from __future__ import annotations

import heapq
import itertools
import math
from array import array
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from .shapes import NARRATION, key_core

T = TypeVar("T")

MIN_SPREAD = 8

NEW_MIN_RISE_Z = 3.0
NEW_MIN_LB = 1.3
LONGSTANDING_MIN_LB = 2.5
LANE_SIZE = 15
#: Keys a lane re-scores as regexes before it settles for fewer than LANE_SIZE.
#: Each attempt scans the whole sentence corpus.
LANE_ATTEMPTS = 2 * LANE_SIZE

#: The two lanes, in the order the Phrase Bank lists them.
NEW = "new"
LONGSTANDING = "longstanding"

SentenceKeys = tuple[str, Iterable[str]]
"""One sentence: its block tag and its keys."""

Reply = tuple[bool, Iterable[SentenceKeys]]
"""One reply: whether it is recent, and its sentences."""


def poisson_upper(k: int, z: float = 1.645) -> float:
    """One-sided 95% upper bound of a Poisson count (Wilson-Hilferty)."""
    a = k + 1
    return a * (1 - 1 / (9 * a) + z / (3 * math.sqrt(a))) ** 3


def overuse_lb(expected: float, observed: int) -> float:
    """Conservative overuse ratio: card-expected over the observed count's upper bound."""
    return expected / poisson_upper(observed)


def rise_z(a: int, a_total: int, b: int, b_total: int, prior: float = 0.5) -> float:
    """Log-odds z of *a* of *a_total* recent characters against *b* of *b_total* older ones."""
    a_rest, b_rest = a_total - a + prior, b_total - b + prior
    recent = math.log((a + prior) / a_rest)
    older = math.log((b + prior) / b_rest)
    return (recent - older) / math.sqrt(1 / (a + prior) + 1 / a_rest + 1 / (b + prior) + 1 / b_rest)


def spread_candidates(characters: Iterable[Iterable[Reply]], min_spread: int = MIN_SPREAD) -> tuple[set[int], int]:
    """Pass 1: hashes of keys used by at least *min_spread* characters.

    Each character's distinct keys become a sorted array of 64-bit string
    hashes and the arrays merge in order, so memory holds hashes rather than
    every key string. String hashes are stable within one process, and a run
    is one process. Returns the candidates and the number of characters.
    """
    arrays: list[array[int]] = []
    for replies in characters:
        seen: set[int] = set()
        for _recent, sentences in replies:
            for _tag, keys in sentences:
                seen.update(map(hash, keys))
        if seen:
            arrays.append(array("q", sorted(seen)))
    runs = itertools.groupby(heapq.merge(*arrays))
    return {value for value, run in runs if sum(1 for _ in run) >= min_spread}, len(arrays)


@dataclass(slots=True)
class KeyCounts:
    """Pass 2's per-key totals across characters."""

    characters: int = 0
    recent_characters: int = 0
    older_characters: int = 0
    rate_sum: dict[str, float] = field(default_factory=dict)
    recent: Counter[str] = field(default_factory=Counter)
    older: Counter[str] = field(default_factory=Counter)


def count_candidates(characters: Iterable[Iterable[Reply]], candidates: set[int]) -> KeyCounts:
    """Pass 2: per-character sentence counts, kept only for candidate keys."""
    out = KeyCounts()
    for replies in characters:
        totals: Counter[str] = Counter()
        hits: Counter[str] = Counter()
        in_recent: set[str] = set()
        in_older: set[str] = set()
        any_recent = any_older = False
        for recent, sentences in replies:
            era = in_recent if recent else in_older
            for tag, keys in sentences:
                totals[tag] += 1
                found = [key for key in keys if hash(key) in candidates]
                hits.update(found)
                era.update(found)
            any_recent |= recent
            any_older |= not recent
        if not totals:
            continue
        out.characters += 1
        out.recent_characters += any_recent
        out.older_characters += any_older
        for key, count in hits.items():
            out.rate_sum[key] = out.rate_sum.get(key, 0.0) + count / totals[key[0]]
        out.recent.update(in_recent)
        out.older.update(in_older)
    return out


def count_card(sentences: Iterable[SentenceKeys], keys: Iterable[str]) -> tuple[Counter[str], Counter[str]]:
    """Card sentences per tag, and card sentences containing each of *keys*."""
    wanted = set(keys)
    totals: Counter[str] = Counter()
    observed: Counter[str] = Counter()
    for tag, sentence_keys in sentences:
        totals[tag] += 1
        observed.update(wanted.intersection(sentence_keys))
    return totals, observed


@dataclass(frozen=True, slots=True)
class KeyStats:
    """What ranks a key. The suggestion's displayed figures come from re-scoring its regex."""

    key: str
    rise_z: float
    lb: float


def key_stats(counts: KeyCounts, card_totals: Mapping[str, int], card_observed: Mapping[str, int]) -> dict[str, KeyStats]:
    """Combine reply rates with card counts into each candidate's statistics."""
    return {
        key: KeyStats(
            key,
            rise_z(counts.recent[key], counts.recent_characters, counts.older[key], counts.older_characters),
            overuse_lb(rate_sum / counts.characters * card_totals.get(key[0], 0), card_observed.get(key, 0)),
        )
        for key, rate_sum in counts.rate_sum.items()
    }


def _overlaps(core: tuple[str, ...], other: tuple[str, ...]) -> bool:
    """Whether one token core equals or contiguously contains the other."""
    short, long = (core, other) if len(core) <= len(other) else (other, core)
    return any(long[i : i + len(short)] == short for i in range(len(long) - len(short) + 1))


def rank_lanes(
    stats: Mapping[str, KeyStats],
    *,
    new_lane: bool,
    skip: Callable[[str], bool] = lambda _key: False,
) -> list[tuple[str, list[KeyStats]]]:
    """Each lane's qualifying keys, best first, as ``(lane, ranked)``.

    New wants a recent rise plus some card-text support; Long-standing wants
    narration with a strong overuse ratio. A key may qualify for both.
    """
    pool = [s for s in stats.values() if not skip(s.key)]
    lanes: list[tuple[str, list[KeyStats]]] = []
    if new_lane:
        ranked = sorted((s for s in pool if s.rise_z > NEW_MIN_RISE_Z and s.lb > NEW_MIN_LB), key=lambda s: (-s.rise_z, s.key))
        lanes.append((NEW, ranked))
    ranked = sorted((s for s in pool if s.key[0] == NARRATION and s.lb > LONGSTANDING_MIN_LB), key=lambda s: (-s.lb, s.key))
    lanes.append((LONGSTANDING, ranked))
    return lanes


def fill_lane(
    ranked: Iterable[KeyStats],
    accept: Callable[[KeyStats], T | None],
    *,
    limit: int = LANE_SIZE,
    attempts: int = LANE_ATTEMPTS,
) -> list[T]:
    """Walk *ranked* best first, keeping what *accept* returns, until *limit* are
    kept or *attempts* keys were tried.

    A key whose core contains, or is contained in, a kept key's core is passed
    over without an attempt. A rejected key blocks nothing, so a lane whose top
    keys fail as regexes fills from further down the ranking.
    """
    kept: list[T] = []
    cores: list[tuple[str, ...]] = []
    tried = 0
    for stats in ranked:
        if len(kept) >= limit or tried >= attempts:
            break
        core = key_core(stats.key)
        if any(_overlaps(core, other) for other in cores):
            continue
        tried += 1
        result = accept(stats)
        if result is not None:
            kept.append(result)
            cores.append(core)
    return kept
