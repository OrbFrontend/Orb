"""One suggestion-mining run, importable by a spawned child process.

The run reads the database read-only, compares model replies with card-authored text, and returns plain dicts. It never writes:
the parent persists the result, and only the accept endpoint turns a suggestion into a phrase-bank entry.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from ...analysis.audit import AUDIT_DEFAULTS
from ...analysis.detectors.contrastive_negation import detect_contrastive_negation
from ...analysis.detectors.negated_narration import detect_negated_narration
from ...analysis.detectors.slop_detector import detect_cliches
from ...analysis.text.roleplay_segmentation import split_segment_sentences
from ...database import iter_model_replies, open_readonly, read_card_rows, read_names
from ...database.models import PhraseGroup, SlopReplyRow, SlopSuggestionDraft
from .patterns import CharacterSentences, PatternScore, SentenceCorpus, Shape, best_pattern, fold
from .scoring import (
    LONGSTANDING,
    NEW,
    KeyStats,
    Reply,
    count_candidates,
    count_card,
    fill_lane,
    key_stats,
    rank_lanes,
    spread_candidates,
)
from .shapes import NARRATION, Sentence, build_names, card_baseline_text, sentence_keys, sentences

RECENT_DAYS = 150
MIN_CHARACTERS = 40
MIN_CARD_NARRATION = 5000
MIN_ERA_CHARACTERS = 40
#: A regex this share of whose examples an enabled scanner already flags is not
#: suggested; the lane fills from the next key instead.
MAX_COVERAGE = 0.5
#: What a key's regex must still show once re-scored as that regex. Looser than
#: the key thresholds in ``scoring``: the regex is a translation of the key, so
#: it is held to "still overused" (and, for New, "still rising"), not re-ranked.
REGEX_FLOOR = {NEW: 1.1, LONGSTANDING: 2.0}
NEW_REGEX_MIN_RISE_Z = 2.0


class MineResult(TypedDict):
    """A run's outcome. A skipped run (``None``) leaves the stored suggestions as they are."""

    status: str
    suggestions: list[SlopSuggestionDraft] | None


def _on(toggles: Mapping[str, object], key: str) -> bool:
    return bool(toggles.get(key, AUDIT_DEFAULTS.get(key, True)))


def coverage(examples: Sequence[str], bank: list[PhraseGroup], toggles: Mapping[str, object]) -> float:
    """The share of *examples* an enabled scanner already flags.

    ``negated_narration`` runs with ``min_hits=0``, one sentence without the two-hits-per-reply gate, so this overstates what
    the Editor flags. It filters out duplicates of existing checks; it promises nothing more.
    """
    checks: list[Callable[[str], bool]] = []
    if _on(toggles, "banned_phrases") and bank:
        checks.append(lambda s: detect_cliches(s, bank).flagged_count > 0)
    if _on(toggles, "contrastive_negation"):
        checks.append(lambda s: bool(detect_contrastive_negation(s)))
    if _on(toggles, "negated_narration"):
        checks.append(lambda s: bool(detect_negated_narration(s, min_hits=0).findings))
    if not examples or not checks:
        return 0.0
    return sum(1 for s in examples if any(check(s) for check in checks)) / len(examples)


def _by_character(rows: Iterable[SlopReplyRow]) -> Iterator[list[SlopReplyRow]]:
    for _key, group in itertools.groupby(rows, key=lambda row: row["character_key"]):
        yield list(group)


def _keyed(replies: list[SlopReplyRow], cutoff: str, names: frozenset[str]) -> Iterator[Reply]:
    for reply in replies:
        yield reply["created_at"] >= cutoff, ((s.tag, sentence_keys(s, names)) for s in sentences(reply["content"]))


def _draft(lane: str, key: str, score: PatternScore, cover: float) -> SlopSuggestionDraft:
    return {
        "key": key,
        "lane": "new" if lane == NEW else "longstanding",
        "label": score.label(),
        "pattern": score.pattern,
        "stats": {
            "spread": score.spread,
            "recent": score.recent,
            "older": score.older,
            "rise_z": round(score.rise_z, 2),
            "card_observed": score.card_observed,
            "card_expected": round(score.card_expected, 1),
            "lb": round(score.lb, 2),
            "coverage": round(cover, 2),
        },
        "fillers": score.filler_summary(),
        # Distinct, so a literal key shows "A beat." once rather than four times.
        "examples": list(dict.fromkeys(score.examples))[:4],
    }


def mine(
    db_path: str,
    bank: list[PhraseGroup],
    audit_toggles: Mapping[str, object],
    dismissed: Iterable[str],
    dismissed_patterns: Iterable[str] = (),
    *,
    now: datetime | None = None,
) -> MineResult:
    """Run the whole miner once. The parent passes everything a run cannot read itself."""
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=RECENT_DAYS)).isoformat()
    dismissed_keys = frozenset(dismissed)
    conn = open_readonly(db_path)
    try:
        cards = read_card_rows(conn)
        baselines = [card_baseline_text(c["first_mes"], c["alternate_greetings"], c["mes_example"]) for c in cards]
        names = build_names(
            [*read_names(conn), *(c["name"] for c in cards)],
            [c["description"] for c in cards],
            [*baselines, *(c["personality"] for c in cards), *(c["scenario"] for c in cards)],
        )
        card_sentences: list[Sentence] = [s for text in baselines for s in sentences(text)]
        narration = sum(1 for s in card_sentences if s.tag == NARRATION)
        if narration < MIN_CARD_NARRATION:
            return _skipped(f"{narration} card narration sentences, {MIN_CARD_NARRATION} needed")

        def replies() -> Iterator[Iterator[Reply]]:
            return (_keyed(group, cutoff, names) for group in _by_character(iter_model_replies(conn)))

        candidates, characters = spread_candidates(replies())
        if characters < MIN_CHARACTERS:
            return _skipped(f"{characters} characters with replies, {MIN_CHARACTERS} needed")
        counts = count_candidates(replies(), candidates)
        del candidates
        card_totals, card_observed = count_card(
            ((s.tag, sentence_keys(s, names)) for s in card_sentences), counts.rate_sum.keys()
        )
        stats = key_stats(counts, card_totals, card_observed)
        new_lane = counts.recent_characters >= MIN_ERA_CHARACTERS and counts.older_characters >= MIN_ERA_CHARACTERS
        lanes = rank_lanes(stats, new_lane=new_lane, skip=dismissed_keys.__contains__)
        del counts, stats

        corpus = _sentence_corpus(conn, baselines, cutoff)
    finally:
        conn.close()

    # A dismissed pattern stays gone even when a different key mines it.
    banked = {g["pattern"] for g in bank if isinstance(g, dict) and g["kind"] == "regex"} | set(dismissed_patterns)
    kept_keys: set[str] = set()
    kept_patterns: set[str] = set()

    def regex_draft(lane: str, key_stat: KeyStats) -> SlopSuggestionDraft | None:
        """The key's suggestion, if its regex holds up on its own and is not already caught."""
        score = best_pattern(Shape.from_key(key_stat.key), corpus)
        if score is None or score.lb < REGEX_FLOOR[lane] or score.pattern in banked | kept_patterns:
            return None
        if lane == NEW and score.rise_z < NEW_REGEX_MIN_RISE_Z:
            return None
        cover = coverage(score.examples, bank, audit_toggles)
        if cover >= MAX_COVERAGE:
            return None
        kept_keys.add(key_stat.key)
        kept_patterns.add(score.pattern)
        return _draft(lane, key_stat.key, score, cover)

    drafts: list[SlopSuggestionDraft] = []
    for lane, ranked in lanes:
        # A key kept by New is not offered again as Long-standing.
        fresh = (s for s in ranked if s.key not in kept_keys)
        drafts.extend(fill_lane(fresh, lambda s, lane=lane: regex_draft(lane, s)))
    era = "" if new_lane else ", New lane off (too few characters in one era)"
    return {"status": f"ok: {len(drafts)} suggestions from {len(corpus.characters)} characters{era}", "suggestions": drafts}


def _skipped(reason: str) -> MineResult:
    return {"status": f"skipped: {reason}", "suggestions": None}


def _sentence_corpus(conn, baselines: Sequence[str], cutoff: str) -> SentenceCorpus:
    """Reply and card sentences as the bank splits them, for re-scoring regexes."""
    characters: list[CharacterSentences] = []
    recent_characters = older_characters = 0
    for group in _by_character(iter_model_replies(conn)):
        chars = CharacterSentences()
        for reply in group:
            recent = reply["created_at"] >= cutoff
            for sentence in split_segment_sentences(reply["content"]):
                chars.add(sentence, recent)
        if chars.sentences:
            characters.append(chars)
            recent_characters += any(chars.recent)
            older_characters += not all(chars.recent)
    card = [s for text in baselines for s in split_segment_sentences(text)]
    return SentenceCorpus(
        characters=characters,
        card=card,
        card_folded=[fold(s) for s in card],
        recent_characters=recent_characters,
        older_characters=older_characters,
    )
