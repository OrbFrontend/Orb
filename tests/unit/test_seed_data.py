"""Sanity checks for the seeded phrase bank.

No database involved: these only validate that every seed entry is a shape the
phrase-bank loader can bind, and that the regex variants actually compile.
"""

import re

from backend.database import SEED_PHRASE_BANK


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
