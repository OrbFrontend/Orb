"""
Tests for contrastive-negation detection.

Organised into:
  - TRUE POSITIVES  – the "AI slop" patterns we *want* to catch
  - FALSE POSITIVES – legitimate English the detector should *ignore*
  - EDGE CASES      – boundary inputs
"""

import pytest

from backend.passes.refine.contrastive_negation import detect_contrastive_negation


# ═══════════════════════════════════════════════════════════════════════════════
# TRUE POSITIVES – rhetorical "not X, but Y" / "isn't X, is Y" slop
# ═══════════════════════════════════════════════════════════════════════════════


class TestTruePositives:
    """These MUST be detected."""

    def test_classic_not_but(self):
        hits = detect_contrastive_negation("It's not a bug, but a feature.")
        assert len(hits) >= 1

    def test_isnt_it_is(self):
        hits = detect_contrastive_negation(
            "This isn't a setback, it is an opportunity."
        )
        assert len(hits) >= 1

    def test_was_not_but(self):
        hits = detect_contrastive_negation("He was not angry, but disappointed.")
        assert len(hits) >= 1

    def test_isnt_simple_mistake(self):
        hits = detect_contrastive_negation(
            "It isn't a simple mistake, it is a catastrophe."
        )
        assert len(hits) >= 1

    def test_not_just_but(self):
        """'not just X, but Y' is the same rhetorical move."""
        hits = detect_contrastive_negation(
            "She is not just talented, she is extraordinary."
        )
        assert len(hits) >= 1

    def test_arent_they_are(self):
        hits = detect_contrastive_negation(
            "They aren't obstacles, they are stepping stones."
        )
        assert len(hits) >= 1

    def test_wasnt_was(self):
        hits = detect_contrastive_negation(
            "It wasn't a failure, it was a learning experience."
        )
        assert len(hits) >= 1

    def test_not_x_but_rather_y(self):
        hits = detect_contrastive_negation(
            "This is not a crisis, but rather an inflection point."
        )
        assert len(hits) >= 1

    def test_doesnt_verb_at_x_verbs_at_y(self):
        """Semicolon-separated 'doesn't VERB at X; VERBs at Y' contrastive pattern."""
        hits = detect_contrastive_negation(
            "She doesn't look at Kai; she looks at the object."
        )
        assert len(hits) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# FALSE POSITIVES – legitimate English that should NOT trigger
# ═══════════════════════════════════════════════════════════════════════════════


class TestFalsePositive_NotOnlyButAlso:
    """'not only … but (also)' is a standard intensifier, not slop."""

    def test_not_only_but_also(self):
        hits = detect_contrastive_negation(
            "Not only is she talented, but she is also kind."
        )
        assert len(hits) == 0

    def test_not_only_inline(self):
        hits = detect_contrastive_negation(
            "He is not only a doctor but also a musician."
        )
        assert len(hits) == 0

    def test_not_only_without_also(self):
        hits = detect_contrastive_negation("This is not only useful but necessary.")
        assert len(hits) == 0


class TestFalsePositive_RegularContrast:
    """Ordinary 'not … but' where X and Y aren't parallel noun/adj phrases."""

    def test_not_like_but_brought(self):
        hits = detect_contrastive_negation(
            "I do not like rain, but I brought an umbrella."
        )
        assert len(hits) == 0

    def test_not_invited_but_came(self):
        hits = detect_contrastive_negation("She was not invited, but she came anyway.")
        assert len(hits) == 0

    def test_not_coming_but_should_go(self):
        hits = detect_contrastive_negation("He's not coming, but we should still go.")
        assert len(hits) == 0

    def test_not_sure_but_think(self):
        hits = detect_contrastive_negation("I'm not sure, but I think it's Thursday.")
        assert len(hits) == 0

    def test_not_ideal_but_works(self):
        hits = detect_contrastive_negation("It's not ideal, but it works.")
        assert len(hits) == 0


class TestFalsePositive_Questions:
    """Questions that happen to contain negated be + later be verb."""

    def test_isnt_where_is(self):
        hits = detect_contrastive_negation("Isn't that strange? Where is the manager?")
        assert len(hits) == 0

    def test_isnt_what_is(self):
        hits = detect_contrastive_negation("Why isn't it working? What is the error?")
        assert len(hits) == 0


class TestFalsePositive_UnrelatedClauses:
    """Be-verb reappears but refers to a different subject or is incidental."""

    def test_isnt_and_there_is(self):
        hits = detect_contrastive_negation(
            "He isn't available, and there is no substitute."
        )
        assert len(hits) == 0

    def test_isnt_but_deadline_is(self):
        hits = detect_contrastive_negation(
            "It isn't done yet, but the deadline is tomorrow."
        )
        assert len(hits) == 0

    def test_wasnt_sure_and_was_confused(self):
        hits = detect_contrastive_negation("She wasn't sure, and he was confused too.")
        assert len(hits) == 0


class TestFalsePositive_NotInOtherConstructions:
    """'not' is part of an infinitive, 'whether or not', etc."""

    def test_told_not_to_go(self):
        hits = detect_contrastive_negation("I told him not to go, but he is stubborn.")
        assert len(hits) == 0

    def test_try_not_to_worry(self):
        hits = detect_contrastive_negation("Try not to worry, but this is serious.")
        assert len(hits) == 0


class TestFalsePositive_DifferentSubjects:
    """Negation + affirmation about completely different things."""

    def test_different_nouns(self):
        hits = detect_contrastive_negation("The car is not red, the house is blue.")
        # Debatable — could be slop-ish, but different subjects = likely not.
        # At minimum is_parallel should be True to match.
        for h in hits:
            if not h["is_parallel"]:
                pytest.fail("Non-parallel different-subject hit is a false positive")


# ═══════════════════════════════════════════════════════════════════════════════
# EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_string(self):
        assert detect_contrastive_negation("") == []

    def test_short_sentence(self):
        assert detect_contrastive_negation("Not bad.") == []

    def test_no_pattern(self):
        assert (
            detect_contrastive_negation("The sky is blue and the grass is green.") == []
        )

    def test_multiple_sentences_one_hit(self):
        text = "The sky is blue. It's not a bug, but a feature. Have a nice day."
        hits = detect_contrastive_negation(text)
        assert len(hits) == 1

    def test_result_has_sentence_field(self):
        hits = detect_contrastive_negation("It's not a bug, but a feature.")
        assert hits and "sentence" in hits[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
