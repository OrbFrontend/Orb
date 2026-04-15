"""
Sanity checks for seed constants in database.py.

These run without a real database — they just validate that every seed
entry has the right Python types so SQLite binding never sees a tuple,
list, or None where a string is expected.
"""
import pytest
from backend.database import SEED_FRAGMENTS, SEED_PHRASE_BANK


STR_FIELDS = ("id", "label", "description", "prompt_text", "negative_prompt")


class TestSeedFragments:
    @pytest.mark.parametrize("fragment", SEED_FRAGMENTS, ids=lambda f: f.get("id", "?"))
    def test_string_fields_are_str(self, fragment):
        for field in STR_FIELDS:
            value = fragment[field]
            assert isinstance(value, str), (
                f"Fragment '{fragment.get('id')}': '{field}' must be str, got {type(value).__name__!r}"
            )



class TestSeedPhraseBank:
    def test_each_group_is_list_of_str(self):
        for i, group in enumerate(SEED_PHRASE_BANK):
            assert isinstance(group, list), f"Group {i} must be a list"
            for j, phrase in enumerate(group):
                assert isinstance(phrase, str), (
                    f"Group {i}[{j}] must be str, got {type(phrase).__name__!r}"
                )
