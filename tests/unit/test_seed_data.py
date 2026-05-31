"""
Sanity checks for seed constants in database.py.

These run without a real database — they just validate that every seed
entry has the right Python types so SQLite binding never sees a tuple,
list, or None where a string is expected.
"""

import re

import pytest
from backend.database import SEED_MOOD_FRAGMENTS, SEED_PHRASE_BANK


STR_FIELDS = ("id", "label", "description", "prompt_text", "negative_prompt")


class TestSeedMoodFragments:
    @pytest.mark.parametrize("fragment", SEED_MOOD_FRAGMENTS, ids=lambda f: f.get("id", "?"))
    def test_string_fields_are_str(self, fragment):
        for field in STR_FIELDS:
            value = fragment[field]
            assert isinstance(
                value, str
            ), f"Mood fragment {fragment.get('id')!r}: {field!r} must be str, got {type(value).__name__!r}"


class TestSeedPhraseBank:
    def test_each_entry_is_a_pattern_str_or_literal_list(self):
        """A seed entry is either a raw regex pattern str or a list of literal variants."""
        for i, entry in enumerate(SEED_PHRASE_BANK):
            if isinstance(entry, str):
                assert entry.strip(), f"Group {i} pattern must be non-empty"
            else:
                assert isinstance(entry, list), f"Group {i} must be a pattern str or a list"
                assert entry, f"Group {i} literal list must be non-empty"
                for j, phrase in enumerate(entry):
                    assert isinstance(phrase, str), f"Group {i}[{j}] must be str, got {type(phrase).__name__!r}"

    def test_regex_patterns_compile(self):
        for entry in SEED_PHRASE_BANK:
            if isinstance(entry, str):
                re.compile(entry)  # raises re.error if malformed
