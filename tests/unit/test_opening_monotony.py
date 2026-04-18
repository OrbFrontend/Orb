"""
Tests for opening_monotony detection.

Organised into:
  - TRUE POSITIVES  – repetitive consecutive sentence openings we *want* to catch
  - FALSE POSITIVES – legitimate variation that should *not* trigger
  - EDGE CASES      – boundary inputs
"""

import pytest

from backend.passes.refine.opening_monotony import detect_opening_monotony


# ═══════════════════════════════════════════════════════════════════════════════
# TRUE POSITIVES – repetitive consecutive openings that should be flagged
# ═══════════════════════════════════════════════════════════════════════════════


class TestTruePositives:
    """These MUST be detected."""

    def test_three_consecutive_same_first_word(self):
        """He X. He Y. He Z."""
        text = "He walked. He ran. He jumped."
        result = detect_opening_monotony(text, n_words=1)
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "he"
        assert flagged.count == 3
        assert flagged.max_run == 3
        assert flagged.fraction == 1.0

    def test_four_consecutive_same_first_word(self):
        """She A. She B. She C. She D."""
        text = "She ate. She slept. She worked. She played."
        result = detect_opening_monotony(text, n_words=1)
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "she"
        assert flagged.count == 4
        assert flagged.max_run == 4
        assert flagged.fraction == 1.0

    def test_three_consecutive_same_two_words(self):
        """He is X. He is Y. He is Z."""
        text = "He is tall. He is strong. He is fast."
        result = detect_opening_monotony(text, n_words=2)
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "he is"
        assert flagged.max_run == 3

    def test_mixed_case_and_punctuation(self):
        """Normalization should treat 'He', 'he', 'He!' as same."""
        text = "He walked! he ran. He jumped?"
        result = detect_opening_monotony(text, n_words=1)
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "he"
        assert flagged.max_run == 3

    def test_repetition_within_longer_text(self):
        """Three consecutive repeated openers surrounded by other sentences."""
        text = (
            "The sky is blue. He walked. He ran. He jumped. "
            "The grass is green. She sang."
        )
        result = detect_opening_monotony(text, n_words=1)
        he_flag = [f for f in result.flagged_openers if f.opener == "he"]
        assert len(he_flag) == 1
        assert he_flag[0].max_run == 3

    def test_repetition_across_paragraphs(self):
        """Consecutive sentences split by newlines."""
        text = "He walked.\n\nHe ran.\n\nHe jumped."
        result = detect_opening_monotony(text, n_words=1)
        assert len(result.flagged_openers) >= 1
        assert result.flagged_openers[0].max_run == 3

    def test_long_sentences_same_first_three_words(self):
        """Longer sentences with identical first three words."""
        text = (
            "The quick brown fox jumps over the lazy dog. "
            "The quick brown fox sleeps all day. "
            "The quick brown fox eats a rabbit."
        )
        result = detect_opening_monotony(text, n_words=3)
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "the quick brown"
        assert flagged.max_run == 3

    def test_long_sentences_same_first_four_words(self):
        """Longer sentences with identical first four words."""
        text = (
            "In the beginning God created the heavens. "
            "In the beginning God created the earth. "
            "In the beginning God created the light."
        )
        result = detect_opening_monotony(text, n_words=4)
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "in the beginning god"
        assert flagged.max_run == 3

    def test_default_n_words_detects_consecutive_first_word(self):
        """Default n_words=1 detects consecutive first-word repetition."""
        text = (
            "She is a very talented artist. "
            "She is a very skilled musician. "
            "She is a very dedicated teacher."
        )
        result = detect_opening_monotony(text)  # default n_words=1
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "she"
        assert flagged.max_run == 3

    def test_line_breaks_and_quotes(self):
        """Sentences with quotes, commas, extra spaces and newlines."""
        text = """He X, "dialogue".  He Y.

He Z something."""
        result = detect_opening_monotony(text, n_words=1)
        assert len(result.flagged_openers) >= 1
        flagged = result.flagged_openers[0]
        assert flagged.opener == "he"
        assert flagged.max_run == 3

    def test_run_in_middle_of_longer_sequence(self):
        """Flag when the 3-in-a-row run is in the middle, not the start."""
        text = "The cat slept. He walked. He ran. He jumped. The dog barked."
        result = detect_opening_monotony(text, n_words=1)
        he_flag = [f for f in result.flagged_openers if f.opener == "he"]
        assert len(he_flag) == 1
        assert he_flag[0].max_run == 3


# ═══════════════════════════════════════════════════════════════════════════════
# FALSE POSITIVES – legitimate variation that should NOT trigger
# ═══════════════════════════════════════════════════════════════════════════════


class TestFalsePositives:
    """These should NOT be flagged."""

    def test_only_two_consecutive(self):
        """Two identical openers in a row — below the threshold of 3."""
        text = "He walked. He ran. The cat slept."
        result = detect_opening_monotony(text, n_words=1)
        assert len(result.flagged_openers) == 0

    def test_non_consecutive_repetition(self):
        """Same opener appears 3+ times but never 3 in a row — should not flag."""
        text = "He walked. The cat slept. He ran. The dog barked. He jumped."
        result = detect_opening_monotony(text, n_words=1)
        assert len(result.flagged_openers) == 0

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
            "report while he describes a sensory nightmare."
        )
        result = detect_opening_monotony(text, n_words=1)
        # After stripping dialogue, narration is: Henderson..., He doesn't look...,
        # He rubs (attribution), He shifts — 3 consecutive 'he' sentences.
        assert len(result.flagged_openers) >= 1
        assert result.flagged_openers[0].opener == "he"
        assert result.flagged_openers[0].max_run >= 3

    def test_different_openers(self):
        """No repetition at all."""
        text = "I went home. She ate pizza. They played games."
        result = detect_opening_monotony(text, n_words=1)
        assert len(result.flagged_openers) == 0

    def test_sentences_shorter_than_n_words(self):
        """If n_words > sentence length, opener is None, no repetition."""
        text = "Hi. Hello. Hey."
        result = detect_opening_monotony(text, n_words=3)
        assert len(result.flagged_openers) == 0
        assert result.all_openers == {}

    def test_varied_openers_same_first_word_different_second(self):
        """He walked. He ran. He jumped — different 2-word openers, not flagged."""
        text = "He walked slowly. He ran fast. He jumped high."
        result = detect_opening_monotony(text, n_words=2)
        assert len(result.flagged_openers) == 0


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
        text = "He walked. The cat slept. He ran. He jumped. He tripped."
        result = detect_opening_monotony(text, n_words=1)
        he_flag = [f for f in result.flagged_openers if f.opener == "he"]
        assert len(he_flag) == 1
        assert he_flag[0].max_run == 3
        assert len(he_flag[0].sentences) == 3
        # Total count includes the first "He walked." too
        assert he_flag[0].count == 4


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIT INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditIntegration:
    def test_audit_flags_narrative_with_dialogue_between_he_sentences(self):
        """Audit correctly flags 'He' run even when dialogue appears between sentences."""
        from backend.passes.refine.audit import run_audit

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
            "report while he describes a sensory nightmare."
        )
        report = run_audit(text, [])
        assert len(report.monotony_result.flagged_openers) >= 1
        assert report.monotony_result.flagged_openers[0].opener == "he"

    def test_audit_flags_consecutive_narrative(self):
        """Audit with default params: 3 consecutive 'He' sentences ARE flagged."""
        from backend.passes.refine.audit import run_audit

        text = (
            "Henderson sighs, a long rattling sound. "
            "He walks to the window. "
            "He stares at the parking lot. "
            "He says nothing for a long time."
        )
        report = run_audit(text, [])
        assert len(report.monotony_result.flagged_openers) >= 1
        flagged = report.monotony_result.flagged_openers[0]
        assert flagged.opener == "he"
        assert flagged.max_run == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
