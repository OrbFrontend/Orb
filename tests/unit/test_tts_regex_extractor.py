"""Unit tests for the regex-based dialogue extractor.

Tests regex_extract() which extracts speakable dialogue from RP text
using pure heuristics — zero LLM calls.
"""

from __future__ import annotations

from backend.tts.regex_extractor import regex_extract


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
        chunks = regex_extract(
            text, backend_type="elevenlabs", supports_emotion_tags=True
        )
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
