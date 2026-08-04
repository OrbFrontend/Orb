"""
Tests for opening_monotony detection.

Organised into:
  - TRUE POSITIVES  – repetitive consecutive sentence openings we *want* to catch
  - FALSE POSITIVES – legitimate variation that should *not* trigger
  - EDGE CASES      – boundary inputs
"""

import pytest

from backend.analysis.detectors.opening_monotony import detect_opening_monotony

# ═══════════════════════════════════════════════════════════════════════════════
# TRUE POSITIVES – repetitive consecutive openings that should be flagged
# ═══════════════════════════════════════════════════════════════════════════════


class TestTruePositives:
    """These MUST be detected."""

    @pytest.mark.parametrize(
        ("text", "n_words", "opener"),
        [
            ("He walked. He ran. He jumped. He skipped.", 1, "he"),
            ("She ate. She slept. She worked. She played.", 1, "she"),
            ("He is tall. He is strong. He is fast. He is quick.", 2, "he is"),
            # Normalization treats 'He', 'he', 'He!' as the same opener.
            ("He walked! he ran. He jumped? He skipped.", 1, "he"),
            (
                "The quick brown fox jumps over the lazy dog. "
                "The quick brown fox sleeps all day. "
                "The quick brown fox eats a rabbit. "
                "The quick brown fox chases the hen.",
                3,
                "the quick brown",
            ),
            (
                "In the beginning God created the heavens. "
                "In the beginning God created the earth. "
                "In the beginning God created the light. "
                "In the beginning God created the stars.",
                4,
                "in the beginning god",
            ),
            # Quotes, commas, extra spaces and newlines between the sentences.
            ('He X, "dialogue".  He Y.\n\nHe Z something.\n\nHe W something.', 1, "he"),
        ],
        ids=[
            "three_consecutive_same_first_word",
            "four_consecutive_same_first_word",
            "same_first_two_words",
            "mixed_case_and_punctuation",
            "long_sentences_same_first_three_words",
            "long_sentences_same_first_four_words",
            "line_breaks_and_quotes",
        ],
    )
    def test_consecutive_run_is_flagged(self, text, n_words, opener):
        result = detect_opening_monotony(text, n_words=n_words)
        assert len(result.flagged_openers) >= 1
        assert result.flagged_openers[0].opener == opener
        assert result.flagged_openers[0].max_run == 4

    def test_flagged_opener_reports_count_and_fraction(self):
        """A whole-text run reports count and fraction alongside max_run."""
        result = detect_opening_monotony("He walked. He ran. He jumped. He skipped.", n_words=1)
        flagged = result.flagged_openers[0]
        assert (flagged.count, flagged.max_run, flagged.fraction) == (4, 4, 1.0)

    def test_repetition_within_longer_text(self):
        """Four consecutive repeated openers surrounded by other sentences."""
        text = "The sky is blue. He walked. He ran. He jumped. He skipped. The grass is green. She sang."
        result = detect_opening_monotony(text, n_words=1)
        he_flag = [f for f in result.flagged_openers if f.opener == "he"]
        assert len(he_flag) == 1
        assert he_flag[0].max_run == 4

    def test_repetition_across_paragraphs(self):
        """Consecutive sentences split by newlines."""
        text = "He walked.\n\nHe ran.\n\nHe jumped.\n\nHe skipped."
        result = detect_opening_monotony(text, n_words=1)
        assert len(result.flagged_openers) >= 1
        assert result.flagged_openers[0].max_run == 4

    def test_default_n_words_detects_consecutive_first_word(self):
        """Default n_words=1 detects consecutive first-word repetition."""
        text = (
            "She is a very talented artist. "
            "She is a very skilled musician. "
            "She is a very dedicated teacher. "
            "She is a very hard worker."
        )
        result = detect_opening_monotony(text)  # default n_words=1
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "she"
        assert flagged.max_run == 4

    def test_run_in_middle_of_longer_sequence(self):
        """Flag when the 4-in-a-row run is in the middle, not the start."""
        text = "The cat slept. He walked. He ran. He jumped. He skipped. The dog barked."
        result = detect_opening_monotony(text, n_words=1)
        he_flag = [f for f in result.flagged_openers if f.opener == "he"]
        assert len(he_flag) == 1
        assert he_flag[0].max_run == 4


# ═══════════════════════════════════════════════════════════════════════════════
# FALSE POSITIVES – legitimate variation that should NOT trigger
# ═══════════════════════════════════════════════════════════════════════════════


class TestFalsePositives:
    """These should NOT be flagged."""

    @pytest.mark.parametrize(
        ("text", "n_words"),
        [
            # Two identical openers in a row — below the threshold of 4.
            ("He walked. He ran. The cat slept.", 1),
            # Same opener 3+ times but never consecutively.
            ("He walked. The cat slept. He ran. The dog barked. He jumped.", 1),
            # No repetition at all.
            ("I went home. She ate pizza. They played games.", 1),
            # Same first word, different second — distinct 2-word openers.
            ("He walked slowly. He ran fast. He jumped high.", 2),
        ],
        ids=[
            "only_two_consecutive",
            "non_consecutive_repetition",
            "different_openers",
            "varied_openers_same_first_word_different_second",
        ],
    )
    def test_not_flagged(self, text, n_words):
        assert detect_opening_monotony(text, n_words=n_words).flagged_openers == []

    def test_realistic_narrative_dialogue_does_not_protect_run(self):
        """Dialogue between 'He' sentences is ignored — the run is still detected."""
        text = (
            "Henderson sighs, a long, rattling sound that suggests he's been "
            "fighting the bureaucracy of the school district for far too long. "
            "He doesn't look at you with any particular interest, just stares "
            "off toward the parking lot, leaning back against the concrete "
            "planter with his clipboard tucked under one arm.\n\n"
            '"Worst, huh?" He rubs the bridge of his nose, his voice flat '
            'and drained of all emotion. "Probably that transfer student '
            "back in '19. Kid from out of state. He presents his ID, right? "
            'The card says..."\n\n'
            "He shifts his weight, his tone remaining as boring as a weather "
            "report while he describes a sensory nightmare. "
            "He waits for a response."
        )
        result = detect_opening_monotony(text, n_words=1)
        # After stripping dialogue, narration is: Henderson..., He doesn't look...,
        # He rubs (attribution), He shifts, He waits — 4 consecutive 'he' sentences.
        assert len(result.flagged_openers) >= 1
        assert result.flagged_openers[0].opener == "he"
        assert result.flagged_openers[0].max_run >= 4

    def test_sentences_shorter_than_n_words(self):
        """If n_words > sentence length, opener is None, no repetition."""
        text = "Hi. Hello. Hey."
        result = detect_opening_monotony(text, n_words=3)
        assert len(result.flagged_openers) == 0
        assert result.all_openers == {}


# ═══════════════════════════════════════════════════════════════════════════════
# EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_string(self):
        result = detect_opening_monotony("")
        assert result.total_sentences == 0
        assert len(result.flagged_openers) == 0
        assert result.monotony_score == 0.0

    def test_single_sentence(self):
        text = "Hello world."
        result = detect_opening_monotony(text, n_words=1)
        assert result.total_sentences == 1
        assert len(result.flagged_openers) == 0
        assert result.monotony_score == 0.0

    def test_only_punctuation(self):
        text = "! ? ."
        result = detect_opening_monotony(text, n_words=1)
        assert result.total_sentences == 3
        assert result.all_openers == {}
        assert len(result.flagged_openers) == 0

    def test_mixed_sentence_lengths(self):
        """Some sentences shorter than n_words, some longer."""
        text = "He. He is. He was."
        result = detect_opening_monotony(text, n_words=2)
        assert result.all_openers == {"he is": 1, "he was": 1}
        assert len(result.flagged_openers) == 0

    def test_min_consecutive_two(self):
        """min_consecutive=2 flags any two in a row."""
        text = "He walked. He ran. The cat slept."
        result = detect_opening_monotony(text, n_words=1, min_consecutive=2)
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "he"
        assert flagged.max_run == 2

    def test_min_consecutive_four(self):
        """min_consecutive=4: three in a row is not enough."""
        text = "He walked. He ran. He jumped. The cat slept."
        result = detect_opening_monotony(text, n_words=1, min_consecutive=4)
        assert len(result.flagged_openers) == 0

    def test_n_words_zero(self):
        """n_words=0 should not crash."""
        text = "He walked."
        detect_opening_monotony(text, n_words=0)

    def test_flagged_sentences_are_the_run(self):
        """FlaggedOpener.sentences holds the consecutive run, not all occurrences."""
        text = "He walked. The cat slept. He ran. He jumped. He tripped. He fell."
        result = detect_opening_monotony(text, n_words=1)
        he_flag = [f for f in result.flagged_openers if f.opener == "he"]
        assert len(he_flag) == 1
        assert he_flag[0].max_run == 4
        assert len(he_flag[0].sentences) == 4
        # Total count includes the first "He walked." too
        assert he_flag[0].count == 5


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIT INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditIntegration:
    def test_audit_flags_narrative_with_dialogue_between_he_sentences(self):
        """Audit correctly flags 'He' run even when dialogue appears between sentences."""
        from backend.analysis import run_audit

        text = (
            "Henderson sighs, a long, rattling sound that suggests he's been "
            "fighting the bureaucracy of the school district for far too long. "
            "He doesn't look at you with any particular interest, just stares "
            "off toward the parking lot, leaning back against the concrete "
            "planter with his clipboard tucked under one arm.\n\n"
            '"Worst, huh?" He rubs the bridge of his nose, his voice flat '
            'and drained of all emotion. "Probably that transfer student '
            "back in '19. Kid from out of state. He presents his ID, right? "
            'The card says..."\n\n'
            "He shifts his weight, his tone remaining as boring as a weather "
            "report while he describes a sensory nightmare. "
            "He waits for a response."
        )
        report = run_audit(text, [])
        assert len(report.monotony_result.flagged_openers) >= 1
        assert report.monotony_result.flagged_openers[0].opener == "he"

    def test_audit_flags_consecutive_narrative(self):
        """Audit with default params: 4 consecutive 'He' sentences ARE flagged."""
        from backend.analysis import run_audit

        text = (
            "Henderson sighs, a long rattling sound. "
            "He walks to the window. "
            "He stares at the parking lot. "
            "He says nothing for a long time. "
            "He waits."
        )
        report = run_audit(text, [])
        assert len(report.monotony_result.flagged_openers) >= 1
        flagged = report.monotony_result.flagged_openers[0]
        assert flagged.opener == "he"
        assert flagged.max_run == 4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
