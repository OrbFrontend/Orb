"""Phrase Bank suggestion miner: keys, bank patterns, and the overuse statistic."""

import re

import pytest

from backend.analysis.detectors.slop_detector import MAX_PHRASE_REGEX
from backend.analysis.text.roleplay_segmentation import split_segment_sentences
from backend.features.slop_suggestions.patterns import LOOSE, STRICT, Shape, build_regex
from backend.features.slop_suggestions.scoring import KeyStats, count_candidates, count_card, fill_lane, key_stats, overuse_lb
from backend.features.slop_suggestions.shapes import sentence_keys, sentences

NO_NAMES: frozenset[str] = frozenset()


def _keys(text: str) -> set[str]:
    return {key for sentence in sentences(text) for key in sentence_keys(sentence, NO_NAMES)}


# -- shapes ------------------------------------------------------------------


def test_despair_sentence_abstracts_to_its_skeleton():
    (sentence,) = sentences("No hate, no anger, just... despair.")
    assert " ".join(sentence.tokens) == "^ no X , no X , just … X ."
    assert "n:no X , no X , just" in sentence_keys(sentence, NO_NAMES)


def test_emphasized_short_sentence_yields_a_literal_narration_key():
    assert "n:= a beat" in _keys("*A beat.*")


def test_quoted_text_is_speech_and_the_rest_narration():
    tagged = {(s.tag, " ".join(s.words)) for s in sentences('"Leave it there," she says, turning away.')}
    assert tagged == {("d", "leave it there"), ("n", "she says turning away")}


def test_towards_folds_to_toward_so_spelling_dialect_is_not_slop():
    assert _keys("She walks towards the door.") & _keys("She walks toward the door.") >= {"n:toward the X"}
    assert re.search(build_regex(Shape.from_key("n:toward the X")), "She walks towards the door.", re.I)


def test_a_name_keeps_a_short_sentence_out_of_the_literal_keys():
    (sentence,) = sentences("Oscar nods.")
    assert not any("=" in key for key in sentence_keys(sentence, frozenset({"oscar"})))


# -- patterns ----------------------------------------------------------------

SOURCES = [
    "*No hate, no anger, just... despair.*",
    "*A beat.*",
    "She leans in close enough that you catch her scent—then pulls back.",
    "The silence doesn't break; it thickens, settling over the room like dust.",
    "With a slow, deliberate motion, she sets the glass down against the bar.",
    "I'm not going anywhere, and you know it… not tonight.",
    "she says, walking towards the door with a half-smile.",
    "Something shifts in his chest.",
]


@pytest.mark.parametrize("source", SOURCES)
def test_every_pattern_is_bank_safe_and_matches_its_source_sentence(source):
    (bank_sentence,) = split_segment_sentences(source)
    keys = _keys(source)
    assert keys
    for key in keys:
        shape = Shape.from_key(key)
        for width in (STRICT, LOOSE):
            pattern = build_regex(shape, width)
            assert len(pattern) <= MAX_PHRASE_REGEX, key
            assert not any(construct in pattern for construct in ("(?P<", "(?<", "(?i")), key
            compiled = re.compile(pattern, re.IGNORECASE)
            # Strict slots hold at most three words; loose ones any run up to 40 chars.
            if width == LOOSE or "X" not in shape.tokens:
                assert compiled.search(bank_sentence), (key, pattern)


# -- scoring -----------------------------------------------------------------


def test_absent_from_cards_counts_only_once_expected_is_large():
    assert overuse_lb(9.6, 0) == pytest.approx(3.23, abs=0.01)
    assert overuse_lb(1.2, 0) < 1


def test_grammar_used_at_equal_rates_scores_about_one():
    key = "n:the X of the X"

    def character(index: int):
        return [(index % 2 == 0, [("n", {key} if i % 10 < 3 else set()) for i in range(100)])]

    counts = count_candidates([character(i) for i in range(50)], {hash(key)})
    card = [("n", {key} if i % 10 < 3 else set()) for i in range(3000)]
    totals, observed = count_card(card, [key])
    assert 0.9 < key_stats(counts, totals, observed)[key].lb < 1.05


def _stats(key: str) -> KeyStats:
    return KeyStats(key, rise_z=0.0, lb=3.0)


def test_a_lane_fills_past_keys_whose_regex_fails():
    ranked = [_stats(k) for k in ("n:a beat X", "n:a beat", "n:the X of", "n:then X", "n:the X of the X", "n:as if")]
    rejected = {"n:a beat X", "n:then X"}
    assert fill_lane(ranked, lambda s: None if s.key in rejected else s.key, limit=3) == ["n:a beat", "n:the X of", "n:as if"]
    assert fill_lane(ranked, lambda s: s.key, limit=3, attempts=1) == ["n:a beat X"]
