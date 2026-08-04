"""
Tests for anti-echo detection — flagging the assistant parroting the user's last
message back as a question.

Organised into:
  - TRUE POSITIVES  – echoes we *want* to catch
  - FALSE POSITIVES – legitimate questions/statements that must *not* trigger
  - EDGE CASES      – boundary inputs
"""

from __future__ import annotations

import pytest

from backend.analysis.detectors.anti_echo import detect_anti_echo

# ═══════════════════════════════════════════════════════════════════════════════
# TRUE POSITIVES
# ═══════════════════════════════════════════════════════════════════════════════


class TestTruePositives:
    def test_quoted_echo_with_repeat_verb(self):
        """The canonical case: a quoted question copying the user's dialogue."""
        result = detect_anti_echo('"Absolutely no money?" She repeats.', '"I have absolutely no money."')
        assert len(result.flagged_echoes) == 1
        flagged = result.flagged_echoes[0]
        assert "absolutely no money" in flagged.matched_phrase
        assert flagged.n_words == 3

    def test_quoted_echo_with_narration_leadin(self):
        """The quote is extracted from its narration lead-in, so only the
        question ("Ice cream?") is flagged — not "He blinks" or the rest."""
        result = detect_anti_echo('He blinks, "Ice cream? You\'re a grown man."', '"I got some ice cream."')
        assert len(result.flagged_echoes) == 1
        assert result.flagged_echoes[0].echo == "Ice cream?"
        assert result.flagged_echoes[0].matched_phrase == "ice cream"

    def test_unquoted_assistant_question_still_caught(self):
        """The *assistant's* echo need not be quoted — an unquoted narration
        question that copies the user's dialogue is still flagged."""
        result = detect_anti_echo("Ice cream? He blinks.", '"I got some ice cream."')
        assert len(result.flagged_echoes) == 1
        assert result.flagged_echoes[0].matched_phrase == "ice cream"

    def test_single_content_word_echo(self):
        """A one-word echo flags when that word carries content."""
        result = detect_anti_echo('"Money?" he asks.', '"I have no money left."')
        assert len(result.flagged_echoes) == 1
        assert result.flagged_echoes[0].matched_phrase == "money"

    def test_echo_of_dialogue_ignores_trailing_ooc(self):
        """An [OOC: ...] aside is dropped, but a genuine echo of the spoken
        line in the same message is still caught."""
        result = detect_anti_echo('"No money?" she repeats.', '"I have absolutely no money." [OOC: keep it tense]')
        assert len(result.flagged_echoes) == 1
        assert result.flagged_echoes[0].matched_phrase == "no money"


# ═══════════════════════════════════════════════════════════════════════════════
# FALSE POSITIVES
# ═══════════════════════════════════════════════════════════════════════════════


class TestFalsePositives:
    @pytest.mark.parametrize(
        ("reply", "user"),
        [
            # A copied word that is a stopword carries no content.
            ('"You?" she says.', '"I think you should leave."'),
            ('"What?" he blinks.', '"What time is it?"'),
            # A long question merely reusing one of the user's nouns is below
            # the coverage threshold.
            (
                '"Should we restock the store room together later?" she wonders.',
                '"I went to the store yesterday."',
            ),
            # Question-gated: a declarative parrot has no '?'.
            ('"No money," he echoes, nodding.', '"I have no money."'),
            # Shares no contiguous run with the user.
            ('"Where are you going?" he asks.', '"I got some ice cream."'),
            # Words in an [OOC: ...] aside are instructions, not in-character
            # speech — reusing them is compliance. "use" leaked only from
            # "Use the phrase ...".
            (
                '"Do you use shells?" she asks.',
                '"I don\'t have money." [OOC: Use the phrase "a mix of"]',
            ),
            # The pool is the user's dialogue only; their narration can't seed a flag.
            ('"Broke?" he asks.', 'I trudge in, broke and tired. "Hey there."'),
            # An all-narration user message has no dialogue to echo.
            ('"Ice cream?" he blinks.', "I got some ice cream."),
        ],
        ids=[
            "bare_stopword_question",
            "wh_word_question",
            "incidental_shared_noun_in_long_question",
            "statement_echo",
            "original_question",
            "ooc_directive_words_not_in_pool",
            "user_narration_not_in_pool",
            "message_with_no_dialogue",
        ],
    )
    def test_not_flagged(self, reply, user):
        assert detect_anti_echo(reply, user).flagged_echoes == []

    def test_run_does_not_bridge_two_utterances(self):
        """Each spoken span is its own run, so "ice cream" can't be assembled
        from "ice" in one utterance and "cream" in the next."""
        result = detect_anti_echo('"Ice cream?" he asks.', '"I sell ice." "Cream is extra."')
        assert all(fe.n_words < 2 for fe in result.flagged_echoes)


# ═══════════════════════════════════════════════════════════════════════════════
# EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_user_message_is_noop(self):
        assert detect_anti_echo('"Ice cream?" he blinks.', "").flagged_echoes == []

    def test_empty_draft_is_noop(self):
        assert detect_anti_echo("", "I got some ice cream.").flagged_echoes == []

    def test_punctuation_only_user_dialogue_is_noop(self):
        assert detect_anti_echo('"Ice cream?" he blinks.', '"...!?"').flagged_echoes == []

    def test_question_mark_with_trailing_marker(self):
        """ "?!" and trailing closing quotes still register as a question."""
        result = detect_anti_echo('"Ice cream?!" he blinks.', '"I got some ice cream."')
        assert len(result.flagged_echoes) == 1
