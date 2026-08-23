"""Unit tests for the regex-based dialogue extractor.

Tests regex_extract() which extracts speakable dialogue from RP text
using pure heuristics — zero LLM calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.workflows.tts.engine.regex_extractor import (
    _extract_beat_action,
    _infer_emotion,
    regex_extract,
)


def test_backend_matches_workflow_extraction_contract():
    fixture = Path(__file__).parents[1] / "fixtures" / "tts_extraction_cases.json"
    for case in json.loads(fixture.read_text(encoding="utf-8")):
        actual = [chunk.spoken_text for chunk in regex_extract(case["text"])]
        assert actual == case["blocks"], case["name"]


class TestSpokenText:
    """spoken_text carries the bare dialogue even when text gains a tag prefix."""

    def test_untagged_spoken_text_equals_text(self):
        chunks = regex_extract('"Hello there."', backend_type="edge", supports_emotion_tags=False)
        assert len(chunks) == 1
        assert chunks[0].text == "Hello there."
        assert chunks[0].spoken_text == "Hello there."

    def test_tagged_text_keeps_spoken_text_bare(self):
        chunks = regex_extract('*laughs* "That is funny."', backend_type="elevenlabs", supports_emotion_tags=True)
        assert len(chunks) == 1
        assert chunks[0].text.startswith("[laugh]")
        assert chunks[0].spoken_text == "That is funny."


class TestBasicDialogue:
    """Extract quoted dialogue from text."""

    def test_simple_double_quoted(self):
        text = 'She looked up. "Hello there," she said.'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hello there,"

    def test_multiple_quotes(self):
        text = '"First." She paused. "Second." Then she added, "Third."'
        chunks = regex_extract(text)
        assert len(chunks) == 3
        assert chunks[0].text == "First."
        assert chunks[1].text == "Second."
        assert chunks[2].text == "Third."

    def test_no_quotes_returns_empty(self):
        text = "*She walks across the room.* The wind howls outside."
        chunks = regex_extract(text)
        assert chunks == []

    def test_empty_string(self):
        assert regex_extract("") == []

    def test_whitespace_only(self):
        assert regex_extract("   \n\t  ") == []


class TestActionBeats:
    """Asterisk action beats are handled correctly."""

    def test_audible_beat_creates_pause(self):
        text = '*she laughs* "That\'s hilarious."'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        # First chunk's pause is zeroed, but the tag/emotion should indicate the beat
        assert "hilarious" in chunks[0].text

    def test_silent_beat_creates_short_pause(self):
        text = '*she smiles* "Come here." *she nods* "Please."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        # Second chunk should have pause from the beat before it
        assert chunks[1].pause_before_ms >= 200

    def test_audible_beat_with_tag(self):
        text = '*she sighs* "Fine."'
        chunks = regex_extract(text, supports_emotion_tags=True)
        assert len(chunks) == 1
        assert "[sigh]" in chunks[0].text

    def test_audible_beat_without_tag(self):
        text = '*she sighs* "Fine."'
        chunks = regex_extract(text, supports_emotion_tags=False)
        assert len(chunks) == 1
        assert "[sigh]" not in chunks[0].text
        assert chunks[0].text == "Fine."

    def test_multiple_beats_between_dialogue(self):
        text = '*she gasps* "What?" *she pauses* "You can\'t be serious."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        assert "What?" in chunks[0].text


class TestParentheticalThoughts:
    """Inner monologue in parens is stripped."""

    def test_thought_stripped(self):
        text = '"Hey." (Maybe I should leave.) "Are you okay?"'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        assert chunks[0].text == "Hey."
        assert chunks[1].text == "Are you okay?"

    def test_only_thoughts_no_dialogue(self):
        text = "(This is interesting.) *She thinks.* (Maybe later.)"
        chunks = regex_extract(text)
        assert chunks == []


class TestEmotionHeuristics:
    """Emotion is inferred from punctuation."""

    def test_exclamation_warm(self):
        text = '"Hey!"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "warm"

    def test_double_exclamation_angry(self):
        text = '"STOP!!"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "angry"

    def test_ellipsis_soft(self):
        text = '"I don\'t know..."'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "soft"

    def test_unicode_punctuation(self):
        assert regex_extract("「止まれ！」")[0].emotion == "warm"
        assert regex_extract("«Attends…»")[0].emotion == "soft"

    def test_surprise_mark(self):
        text = '"What?!"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "surprised"

    def test_neutral_default(self):
        text = '"Okay."'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "neutral"

    def test_all_caps_angry(self):
        text = '"NOPE"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "angry"

    def test_emotion_from_beat(self):
        text = '*she whispers* "Come closer."'
        chunks = regex_extract(text)
        # Whisper beat should set emotion to whispered
        assert chunks[0].emotion in ("whispered", "neutral")


class TestBackendAwareness:
    """Backend type affects tag output."""

    def test_edge_no_tags(self):
        text = '*she laughs* "Funny."'
        chunks = regex_extract(text, backend_type="edge", supports_emotion_tags=False)
        assert "[laugh]" not in chunks[0].text

    def test_elevenlabs_with_tags(self):
        text = '*she laughs* "Funny."'
        chunks = regex_extract(text, backend_type="elevenlabs", supports_emotion_tags=True)
        assert "[laugh]" in chunks[0].text


class TestEdgeCases:
    """Tricky inputs."""

    def test_dialogue_with_inner_quotes(self):
        # Single quotes inside double quotes should be preserved
        text = "\"She said 'hello' to me.\""
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert "hello" in chunks[0].text

    def test_very_long_text(self):
        dialogue = '"' + "A" * 5000 + '"'
        chunks = regex_extract(dialogue)
        assert len(chunks) == 1
        assert len(chunks[0].text) == 5000

    def test_mixed_beats_and_dialogue_complex(self):
        text = (
            '*The door creaks open.* "Hey." *she smiles warmly* '
            '"I was just thinking about you." (God, he looks tired.) '
            '"You okay?" *she reaches out*'
        )
        chunks = regex_extract(text)
        assert len(chunks) == 3
        assert chunks[0].text == "Hey."
        assert "thinking about you" in chunks[1].text
        assert chunks[2].text == "You okay?"

    def test_pause_between_consecutive_lines(self):
        text = '"First." "Second." "Third."'
        chunks = regex_extract(text)
        assert len(chunks) == 3
        # First chunk should have no pause_before
        assert chunks[0].pause_before_ms == 0
        # Later chunks should have some pause
        assert chunks[1].pause_before_ms >= 300


# ---------------------------------------------------------------------------
# Direct tests of the private helpers, and the branches the public path hides
# ---------------------------------------------------------------------------


class TestInferEmotionDirect:
    """Direct tests for _infer_emotion(): the rstrip(" '\"") that must run before
    the end-of-string punctuation check."""

    def test_trailing_quote_stripped_before_check(self):
        # The rstrip(" '\"") must strip the closing quote so ! is the end char
        assert _infer_emotion("Stop!'") == "warm"


class TestExtractBeatActionDirect:
    """Direct tests for _extract_beat_action(). Verifies it picks the right
    verb from beat text and distinguishes audible from silent beats."""

    def test_known_audible_word(self):
        assert _extract_beat_action("she laughs softly") == "laughs"

    def test_silent_beat_returns_empty(self):
        # "smiles" is not in AUDIBLE_BEATS or AUDIBLE_EMOTION_MAP
        assert _extract_beat_action("she smiles") == ""


class TestParentheticalRemoval:
    """Parenthetical text is removed before dialogue extraction."""

    def test_parenthetical_inside_beat_asterisks_preserved(self):
        # Parentheses inside asterisks are beat text, not thoughts
        text = '*she (quietly) sighs* "Hey."'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hey."


class TestBeatEmotionAndTagPropagation:
    """Beat metadata (emotion, tag) propagates to SpeakableChunks correctly."""

    def test_strong_text_emotion_beats_beat_emotion(self):
        # Text has !! (angry), beat is sighs (soft). Text emotion wins
        # because beat_emotion only applies when emotion == "neutral".
        text = '*she sighs* "STOP!!"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "angry"

    def test_silent_beat_no_tag_ever(self):
        text = '*she smiles* "Hey."'
        chunks = regex_extract(text, supports_emotion_tags=True)
        assert "[" not in chunks[0].text


class TestEmDashDialogue:
    """Em-dash dialogue (—text—) used as fallback when no double quotes."""

    def test_emdash_dialogue_extracted(self):
        text = "—Hello there.—"
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hello there."

    def test_emdash_fallback_only_when_no_quotes(self):
        # Double quotes take priority — em-dashes inside quotes are preserved
        # as part of the dialogue text (they're just punctuation)
        text = '"She said — yes — to me."'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert "She said" in chunks[0].text and "to me." in chunks[0].text


class TestEmphasisAsterisksPreserved:
    """Asterisks inside quoted dialogue are emphasis, not beats."""

    def test_asterisk_inside_quotes_not_beat(self):
        text = '"I *really* mean it."'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert chunks[0].text == "I *really* mean it."


class TestEmptyDialogueSkipped:
    """Empty quoted strings produce no chunks (continue, not break)."""

    def test_whitespace_only_quote_skipped_not_break(self):
        # A quoted string that's only whitespace after strip() → skipped.
        # The next real line must still appear.
        # Using text that doesn't trigger the "" adjacent-match issue:
        text = 'Some text. "   " and then "Real dialogue here."'
        chunks = regex_extract(text)
        # The whitespace-only quote is skipped, real dialogue survives
        assert any("Real dialogue" in c.text for c in chunks)


class TestBeatConsumed:
    """A beat is consumed (set to None) after being applied to one dialogue.
    It should NOT bleed into subsequent dialogue lines."""

    def test_beat_consumed_after_first_dialogue(self):
        text = '*she gasps* "First." "Second."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        # First chunk gets the gasp beat emotion (surprised)
        assert chunks[0].emotion == "surprised"
        # Second chunk: beat was consumed, neutral text → inter-dialogue pause only
        assert chunks[1].pause_before_ms == 300


class TestInferEmotionAllCaps:
    """Specifically tests the stripped.isupper() branch."""

    def test_all_caps_exactly_four_chars(self):
        # len > 3, so "STOP" (4 chars) → angry
        assert _infer_emotion("STOP") == "angry"

    def test_all_caps_three_chars_not_angry(self):
        # len <= 3, so "HEY" → neutral (not angry)
        assert _infer_emotion("HEY") == "neutral"


class TestExtractBeatActionConjugation:
    """Tests the rstrip("s") and rstrip("ed") fallback paths."""

    def test_conjugation_ed_stripping(self):
        assert _extract_beat_action("he moaned") == "moan"

    def test_no_false_positive_conjugation(self):
        # "dances" → rstrip("s") → "dance" → not in BEATS
        assert _extract_beat_action("she dances") == ""


class TestInternalDictKeysUsed:
    """The beat dict keys 'action', 'is_audible', 'emotion', 'tag' are read
    back later in the pipeline. These tests verify the full pipeline uses them."""

    def test_action_key_affects_is_audible(self):
        # An audible beat (*laughs*) → is_audible=True → 400ms pause
        # A silent beat (*smiles*) → is_audible=False → 200ms pause
        text = '"One." *she laughs* "Two." *she smiles* "Three."'
        chunks = regex_extract(text)
        assert len(chunks) == 3
        assert chunks[1].pause_before_ms == 400  # audible
        assert chunks[2].pause_before_ms == 200  # silent
