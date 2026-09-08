"""The curated vocabulary's normalization, identity, and diff rules.

Pure functions, no app stack. These are the rules the whole idempotency contract
rests on: the hash decides which cards are pending, and the diff decides whether
a save costs zero model calls or a full re-pass over the library.
"""

from __future__ import annotations

from backend.features.library_tags import (
    MAX_TAG_LENGTH,
    MAX_VOCABULARY,
    normalize_vocabulary,
    vocabulary_hash,
    vocabulary_revision,
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


def test_hash_ignores_display_casing():
    assert vocabulary_hash(["NSFW"]) == vocabulary_hash(["Nsfw"])


def test_hash_ignores_reorder():
    assert vocabulary_hash(["Fantasy", "Romance"]) == vocabulary_hash(["Romance", "Fantasy"])


def test_exact_revision_changes_on_recasing_and_reorder():
    assert vocabulary_revision(["NSFW"]) != vocabulary_revision(["Nsfw"])
    assert vocabulary_revision(["Fantasy", "Romance"]) != vocabulary_revision(["Romance", "Fantasy"])
