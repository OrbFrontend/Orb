"""Adversarial contracts for the shared word-level lexical primitives."""

import pytest

from backend.analysis.text.lexical import (
    count_content_words,
    count_words,
    is_contiguous_subsequence,
    ngrams,
    normalize_word,
    tokenize,
)


def test_tokenize_keeps_unicode_words_instead_of_clipping_them():
    assert tokenize("Élodie drank café in 東京") == ["élodie", "drank", "café", "in", "東京"]


def test_tokenize_normalizes_decomposed_unicode_before_matching():
    assert tokenize("Cafe\u0301") == ["café"]


def test_tokenize_canonicalizes_curly_apostrophes_for_stopwords():
    tokens = tokenize("I don’t know")
    assert tokens == ["i", "don't", "know"]
    assert count_content_words(tokens) == 1


def test_tokenize_drops_standalone_quote_marks_and_repeated_apostrophes():
    assert tokenize("'quoted' rock 'n' roll foo''bar") == ["quoted", "rock", "n", "roll", "foo", "bar"]


def test_normalize_word_uses_the_same_unicode_alphabet_as_tokenize():
    assert normalize_word("(DÉJÀ!)") == "déjà"
    assert normalize_word("well—known") == "wellknown"
    assert normalize_word("don’t") == "don't"


@pytest.mark.parametrize("size", [0, -1])
def test_ngrams_rejects_nonpositive_window_sizes(size: int):
    with pytest.raises(ValueError, match="positive"):
        list(ngrams(["a", "b"], size))


def test_empty_sequence_is_not_reported_as_a_contained_phrase():
    assert is_contiguous_subsequence((), ("anything",)) is False


@pytest.mark.parametrize(
    "text", ["", 'She said, "Wait" — then left.', "*He waits.*\n\n  well-known  ... don’t", "Élodie drank café"]
)
def test_count_words_matches_whitespace_split_without_cjk(text):
    assert count_words(text) == len(text.split())


def test_count_words_counts_each_han_and_kana_character():
    assert count_words("*他走进房间。*\n\n“你来了。”她说。") == 10
    assert count_words("ありがとう、カード") == 8
    assert count_words("OK，他说 fine") == 4
