"""The curated vocabulary's normalization, identity, and diff rules.

Pure functions, no app stack. These are the rules the whole idempotency contract
rests on: the hash decides which cards are pending, and the diff decides whether
a save costs zero model calls or a full re-pass over the library.
"""

from __future__ import annotations

from backend.features.library_tags import (
    MAX_TAG_LENGTH,
    MAX_VOCABULARY,
    diff_vocabulary,
    normalize_vocabulary,
    vocabulary_hash,
)


def test_names_are_trimmed_and_whitespace_collapsed():
    assert normalize_vocabulary(["  Fantasy ", "Slow\n\tburn"]) == ["Fantasy", "Slow burn"]


def test_empty_names_are_dropped():
    assert normalize_vocabulary(["Fantasy", "   ", "", "\n"]) == ["Fantasy"]


def test_dedupe_is_case_insensitive_and_keeps_the_first_casing():
    # Both spellings would otherwise spend two of the 64 slots on one concept and
    # split its assignments across them.
    assert normalize_vocabulary(["Fantasy", "fantasy", "FANTASY"]) == ["Fantasy"]


def test_pipe_is_stripped_because_it_delimits_the_filter_attribute():
    # data-tags is "|a|b|"; a tag carrying a pipe would match as two.
    assert normalize_vocabulary(["Sci|Fi"]) == ["SciFi"]


def test_names_are_capped_and_retrimmed_at_the_boundary():
    long = "x" * (MAX_TAG_LENGTH + 20)
    assert normalize_vocabulary([long]) == ["x" * MAX_TAG_LENGTH]
    # A cut landing on a space must not leave a trailing one behind.
    assert normalize_vocabulary(["y" * (MAX_TAG_LENGTH - 1) + " zzz"]) == ["y" * (MAX_TAG_LENGTH - 1)]


def test_the_list_is_capped_at_the_ceiling():
    result = normalize_vocabulary([f"tag{i}" for i in range(MAX_VOCABULARY + 30)])
    assert len(result) == MAX_VOCABULARY
    assert result[0] == "tag0" and result[-1] == f"tag{MAX_VOCABULARY - 1}"


def test_non_strings_are_ignored_rather_than_crashing():
    assert normalize_vocabulary(["Fantasy", None, 7, {"a": 1}]) == ["Fantasy"]  # type: ignore[list-item]


def test_hash_is_stable_for_the_same_vocabulary():
    assert vocabulary_hash(["Fantasy", "Romance"]) == vocabulary_hash(["Fantasy", "Romance"])


def test_hash_is_case_sensitive():
    # The canonical casing is stored on every card and shown in the chip row, so
    # re-casing a tag is a real edit even though it adds no concept.
    assert vocabulary_hash(["NSFW"]) != vocabulary_hash(["Nsfw"])


def test_hash_changes_on_reorder():
    # Order is part of the identity; the caller's diff is what keeps a reorder
    # from costing model calls (nothing added ⇒ bump, don't re-run).
    assert vocabulary_hash(["Fantasy", "Romance"]) != vocabulary_hash(["Romance", "Fantasy"])


def test_diff_reports_an_addition():
    added, removed = diff_vocabulary(["Fantasy"], ["Fantasy", "Romance"])
    assert added == {"Romance"} and removed == set()


def test_diff_reports_a_removal():
    added, removed = diff_vocabulary(["Fantasy", "Romance"], ["Fantasy"])
    assert added == set() and removed == {"Romance"}


def test_a_rename_is_both_a_removal_and_an_addition():
    # Which is correct: the old name must be pruned everywhere, and the new one
    # has never been offered to any card, so every card is pending again.
    added, removed = diff_vocabulary(["Sci-Fi"], ["Science Fiction"])
    assert added == {"Science Fiction"} and removed == {"Sci-Fi"}


def test_a_reorder_is_neither():
    added, removed = diff_vocabulary(["Fantasy", "Romance"], ["Romance", "Fantasy"])
    assert added == set() and removed == set()


def test_a_recasing_is_neither():
    # It changes the hash, which restamps the rows; it cannot make a card
    # eligible for a tag it was not already offered.
    added, removed = diff_vocabulary(["NSFW"], ["Nsfw"])
    assert added == set() and removed == set()
