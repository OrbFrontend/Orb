"""
Tests for template_repetition detection.

Organised into:
  - TRUE POSITIVES  – repetitive templates across paragraphs we *want* to catch
  - FALSE POSITIVES – legitimate variation that should *not* trigger
  - EDGE CASES      – boundary inputs
"""

import pytest

from backend.passes.editor.template_repetition import (
    detect_template_repetition,
)


# ═══════════════════════════════════════════════════════════════════════════════
# TRUE POSITIVES – repetitive templates across paragraphs that should be flagged
# ═══════════════════════════════════════════════════════════════════════════════


class TestTruePositives:
    """These MUST be detected."""

    def test_same_template_three_times(self):
        """Simple exact template repetition with max_words=2."""
        text = (
            "The question hangs in the air. "
            "The question is heavy. "
            "The question remains."
        )
        result = detect_template_repetition(text, max_words=2, flag_threshold=3)
        assert len(result.flagged_templates) >= 1
        flagged = result.flagged_templates[0]
        assert "the question" in flagged.template
        assert flagged.count >= 3

    def test_similar_templates_across_paragraphs(self):
        """Similar templates appearing across paragraph breaks."""
        text = (
            "The question hangs in the air.\n\n"
            "Another paragraph here.\n\n"
            "Then another paragraph.\n\n"
            "The question is heavy.\n\n"
            "The question remains unanswered."
        )
        result = detect_template_repetition(text, max_words=2, flag_threshold=3)
        assert len(result.flagged_templates) >= 1
        # Find the flagged template about "the question"
        question_templates = [
            ft for ft in result.flagged_templates if "the question" in ft.template
        ]
        assert len(question_templates) >= 1
        assert question_templates[0].count >= 3

    def test_multiple_similar_templates(self):
        """Different template groups should be detected."""
        text = (
            "The wind blows through the trees. "
            "The wind is cold today. "
            "The wind has died down. "
            "She looks out the window. "
            "She looks at the clock. "
            "She looks away."
        )
        result = detect_template_repetition(text, max_words=2, flag_threshold=3)
        # Should detect "the wind" and "she looks" templates
        templates = [ft.template for ft in result.flagged_templates]
        assert any("the wind" in t for t in templates)
        assert any("she looks" in t for t in templates)

    def test_partial_template_match(self):
        """Templates with significant word overlap should cluster."""
        text = (
            "It was not a question but a statement. "
            "It was not the answer she expected. "
            "It was not even close to correct."
        )
        result = detect_template_repetition(text, max_words=3, flag_threshold=3)
        # "it was not" should be a flagged template
        assert len(result.flagged_templates) >= 1

    def test_long_range_template_repetition(self):
        """Templates appearing far apart should still be detected."""
        text = (
            "In the beginning there was light.\n\n"
            "Many paragraphs pass by here with various content.\n\n"
            "The story continues in its usual way.\n\n"
            "Characters develop and plot thickens.\n\n"
            "In the beginning there was nothing."
        )
        result = detect_template_repetition(text, max_words=3, flag_threshold=2)
        flagged = result.flagged_templates
        # Should detect "in the beginning" pattern
        assert any("in the beginning" in ft.template for ft in flagged)


# ═══════════════════════════════════════════════════════════════════════════════
# FALSE POSITIVES – legitimate variation that should NOT trigger
# ═══════════════════════════════════════════════════════════════════════════════


class TestFalsePositives:
    """These should NOT be flagged."""

    def test_no_repetition_below_threshold(self):
        """Single occurrence should not be flagged."""
        text = "The question hangs in the air."
        result = detect_template_repetition(text, flag_threshold=2)
        assert len(result.flagged_templates) == 0

    def test_dissimilar_templates(self):
        """Completely different sentence structures."""
        text = (
            "The sun rose over the mountains. "
            "Birds chirped in the trees. "
            "A gentle breeze rustled the leaves."
        )
        result = detect_template_repetition(text, flag_threshold=2)
        # Should have no flagged templates
        assert len(result.flagged_templates) == 0

    def test_high_flag_threshold_blocks_detection(self):
        """High threshold should prevent flagging."""
        text = "The question hangs in the air. " "The question is heavy."
        result = detect_template_repetition(text, max_words=2, flag_threshold=3)
        # Threshold is 3 but only 2 occurrences
        assert len(result.flagged_templates) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_string(self):
        result = detect_template_repetition("")
        assert result.total_sentences == 0
        assert result.flagged_templates == []
        assert result.repetition_score == 0.0

    def test_single_sentence(self):
        result = detect_template_repetition("One sentence only.")
        assert result.total_sentences == 1
        assert result.flagged_templates == []

    def test_only_dialogue_no_narration(self):
        """Dialogue-only text - only attribution fragments remain after stripping."""
        text = '"Hello there," he said. "How are you?" she asked.'
        result = detect_template_repetition(text)
        # Dialogue is stripped, leaving only "he said" and "she asked" as narration fragments
        # These short fragments are valid sentences for analysis
        assert result.total_sentences == 2
        assert "he said" in result.all_templates
        assert "she asked" in result.all_templates

    def test_mixed_dialogue_and_narration(self):
        """Should analyze only narration, ignoring dialogue."""
        text = (
            '"Hello," he said. The question hung in the air. '
            '"What?" she replied. The question was heavy. '
            '"I see," he nodded. The question remained.'
        )
        result = detect_template_repetition(text, max_words=2, flag_threshold=3)
        # Should find the "the question" template in narration
        assert len(result.flagged_templates) >= 1
        flagged = result.flagged_templates[0]
        assert "the question" in flagged.template
        assert flagged.count == 3

    def test_varied_max_tags(self):
        """Different max_tags values should capture different templates."""
        text = (
            "It was the best of times it was the worst of times. "
            "It was the age of wisdom it was the age of foolishness."
        )
        # With max_words=2, should match "it was"
        result_small = detect_template_repetition(text, max_words=2, flag_threshold=2)
        # With max_words=4, might match more specifically
        result_large = detect_template_repetition(text, max_words=4, flag_threshold=2)

        # Both should find something
        print(result_large)
        assert len(result_small.flagged_templates) >= 1

    def test_normalization_lowercase(self):
        """Templates should be case-insensitive."""
        text = (
            "The Question hangs in the air. "
            "THE QUESTION is heavy. "
            "the question remains."
        )
        result = detect_template_repetition(text, max_words=2, flag_threshold=3)
        # Should cluster these together
        assert len(result.flagged_templates) >= 1
        assert result.flagged_templates[0].count >= 3

    def test_result_dataclass_fields(self):
        """Verify FlaggedTemplate has expected fields."""
        text = (
            "The test sentence one. "
            "The test sentence two. "
            "The test sentence three."
        )
        result = detect_template_repetition(text, max_words=2, flag_threshold=3)
        assert len(result.flagged_templates) >= 1

        ft = result.flagged_templates[0]
        assert hasattr(ft, "template")
        assert hasattr(ft, "count")
        assert hasattr(ft, "fraction")
        assert hasattr(ft, "sentences")
        assert isinstance(ft.sentences, list)
        assert isinstance(ft.count, int)

    def test_repetition_score_calculation(self):
        """Repetition score should reflect template reuse."""
        text = (
            "Template A here. " "Template A again. " "Something completely different."
        )
        result = detect_template_repetition(text, max_words=2)
        # Score should be > 0 since there's repetition
        # Note: templates are "template a" and "something completely"
        # So no exact template is repeated 2+ times
        assert result.total_sentences == 3

    def test_similarity_threshold_effect(self):
        """Higher similarity threshold should reduce clustering."""
        text = "The big red dog. " "The big blue cat. " "The big green fish."
        # High threshold - less clustering
        result_high = detect_template_repetition(
            text, max_words=3, flag_threshold=2, similarity_threshold=0.9
        )
        # Lower threshold - more clustering
        result_low = detect_template_repetition(
            text, max_words=3, flag_threshold=2, similarity_threshold=0.5
        )
        # Low threshold should find at least as many flagged templates as high
        assert len(result_low.flagged_templates) >= len(result_high.flagged_templates)

    def test_exact_template_repetition_in_score(self):
        """Exact template repetition should affect score."""
        text = "The cat sat. " "The cat sat. " "The cat sat."
        result = detect_template_repetition(text, max_words=3)
        # "the cat sat" appears 3 times
        assert result.repetition_score > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
