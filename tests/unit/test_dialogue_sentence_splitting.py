"""
Tests for dialogue-aware sentence splitting in slop_detector and contrastive_negation.

Root issue: the naive (?<=[.!?])\\s+ lookbehind fails when a closing quote sits
between the sentence-ending punctuation and the following space (e.g. !" or ?").
The action tag after dialogue ("she screamed, her voice cracking.") gets fused
into the same sentence as the preceding dialogue, so banned phrases inside it
are reported with an unusably long sentence context that the editor can't locate
in the draft.
"""

from backend.analysis.audit import format_report, run_audit
from backend.analysis.detectors.contrastive_negation import (
    _split_sentences as neg_split,
)
from backend.analysis.detectors.slop_detector import _split_sentences as slop_split
from backend.analysis.detectors.slop_detector import detect_cliches

# ═══════════════════════════════════════════════════════════════════════════════
# slop_detector._split_sentences
# ═══════════════════════════════════════════════════════════════════════════════


class TestSlopSplitterDialogueQuotes:
    def test_exclamation_straight_quote_splits(self):
        """!" (straight quote) followed by space must be a sentence boundary."""
        sentences = slop_split('"Stop it!" she cried.')
        assert len(sentences) == 2
        assert "she cried." in sentences

    def test_question_straight_quote_splits(self):
        """?" (straight quote) followed by space must be a sentence boundary."""
        sentences = slop_split('"Are you sure?" he asked.')
        assert len(sentences) == 2
        assert "he asked." in sentences

    def test_period_straight_quote_splits(self):
        """." (straight quote) followed by space must be a sentence boundary."""
        sentences = slop_split('"I am done." She turned away.')
        assert len(sentences) == 2
        assert "She turned away." in sentences

    def test_exclamation_curly_quote_splits(self):
        """” (curly right quote) after ! must be a sentence boundary."""
        sentences = slop_split("“Stop it!” she cried.")
        assert len(sentences) == 2
        assert "she cried." in sentences

    def test_question_curly_quote_splits(self):
        """” (curly right quote) after ? must be a sentence boundary."""
        sentences = slop_split("“Are you sure?” he asked.")
        assert len(sentences) == 2
        assert "he asked." in sentences

    def test_action_tag_after_multi_sentence_dialogue(self):
        """Action tag following a closing !" must be its own isolated sentence."""
        text = (
            '"I want your name! I want your employee number! I want whoever is responsible!" she screamed, her voice cracking.'
        )
        sentences = slop_split(text)
        assert "she screamed, her voice cracking." in sentences
        assert not any("she screamed" in s and "I want" in s for s in sentences)

    def test_banned_phrase_in_action_tag_isolated(self):
        """A banned phrase in the action tag is reported in the action-tag sentence, not the dialogue blob."""
        text = (
            '"I want your name! I want your employee number! I want whoever is responsible!" she screamed, her voice cracking.'
        )
        result = detect_cliches(text, [["voice cracking"]])
        assert result.flagged_count == 1
        flagged_sentence = result.flagged_sentences[0].sentence
        assert "voice cracking" in flagged_sentence
        assert "I want your name" not in flagged_sentence

    def test_plain_period_split_unaffected(self):
        """Ordinary '. ' boundaries still split correctly."""
        sentences = slop_split("He walked. She ran. They stopped.")
        assert sentences == ["He walked.", "She ran.", "They stopped."]

    def test_exclamation_no_quote_unaffected(self):
        """'! ' without a following quote still splits correctly."""
        sentences = slop_split("He yelled! She ran. They stopped.")
        assert sentences == ["He yelled!", "She ran.", "They stopped."]

    def test_mid_sentence_quote_separates_segments(self):
        """A quoted word inside a sentence is its own segment — dialogue and
        narration never share a segment, and each segment is a contiguous
        substring of the source (the editor's patching relies on that)."""
        text = 'She said "hello" to him. He nodded.'
        sentences = slop_split(text)
        assert sentences == ["She said", '"hello"', "to him.", "He nodded."]
        assert all(s in text for s in sentences)


# ═══════════════════════════════════════════════════════════════════════════════
# contrastive_negation._split_sentences — same boundary cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestContrastiveNegationSplitterDialogueQuotes:
    def test_exclamation_straight_quote_splits(self):
        sentences = neg_split('"Stop it!" she cried.')
        assert len(sentences) == 2
        assert "she cried." in sentences

    def test_question_straight_quote_splits(self):
        sentences = neg_split('"Are you sure?" he asked.')
        assert len(sentences) == 2
        assert "he asked." in sentences

    def test_period_straight_quote_splits(self):
        sentences = neg_split('"I am done." She turned away.')
        assert len(sentences) == 2
        assert "She turned away." in sentences

    def test_exclamation_curly_quote_splits(self):
        sentences = neg_split("“Stop it!” she cried.")
        assert len(sentences) == 2
        assert "she cried." in sentences

    def test_question_curly_quote_splits(self):
        sentences = neg_split("“Are you sure?” he asked.")
        assert len(sentences) == 2
        assert "he asked." in sentences

    def test_action_tag_after_multi_sentence_dialogue(self):
        text = (
            '"I want your name! I want your employee number! I want whoever is responsible!" she screamed, her voice cracking.'
        )
        sentences = neg_split(text)
        assert "she screamed, her voice cracking." in sentences
        assert not any("she screamed" in s and "I want" in s for s in sentences)

    def test_plain_period_split_unaffected(self):
        sentences = neg_split("He walked. She ran. They stopped.")
        assert sentences == ["He walked.", "She ran.", "They stopped."]

    def test_mid_sentence_quote_no_spurious_split(self):
        sentences = neg_split('She said "hello" to him. He nodded.')
        assert sentences == ['She said "hello" to him.', "He nodded."]


# ═══════════════════════════════════════════════════════════════════════════════
# Audit report — reported dialogue snippet must not carry a dangling quote
# ═══════════════════════════════════════════════════════════════════════════════


class TestReportStripsDanglingQuotes:
    def test_banned_phrase_in_dialogue_reported_without_dangling_quote(self):
        # The splitter keeps the opening `"` but eats the closing one, so the raw
        # snippet is `"…vulnerability.` — the report must strip the outer quote so
        # the model copies a search string it can locate in the draft.
        draft = '"Do not mistake my compliance for vulnerability." She remains still.'
        report = format_report(run_audit(draft, [["vulnerability"]]))
        assert "Do not mistake my compliance for vulnerability." in report
        assert '"Do not mistake' not in report

    def test_apostrophe_survives_in_report(self):
        # Straight ' is not an outer marker — contractions must stay intact.
        draft = "She said the plan wouldn't fail this time."
        report = format_report(run_audit(draft, [["wouldn't fail"]]))
        assert "wouldn't fail" in report
