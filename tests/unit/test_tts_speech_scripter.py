"""Unit tests for the TTS speech scripter module.

Tests the speech scripter output parsing (_parse_chunks) and the
fallback dialogue extractor (_fallback_passthrough) without hitting
any LLM API.
"""

from __future__ import annotations

from backend.tts.speech_scripter import _fallback_passthrough, _parse_chunks


class TestParseChunks:
    """_parse_chunks: extract SpeakableChunk list from LLM JSON output."""

    def test_valid_json_array(self):
        text = '[{"text":"Hello there.","emotion":"warm"}]'
        chunks = _parse_chunks(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hello there."
        assert chunks[0].emotion == "warm"

    def test_json_inside_code_fence(self):
        text = '```json\n[{"text":"Hey.","emotion":"neutral"}]\n```'
        chunks = _parse_chunks(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hey."

    def test_json_with_surrounding_text(self):
        text = 'Here are the chunks:\n[{"text":"Hi.","emotion":"playful"}]\nDone.'
        chunks = _parse_chunks(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hi."

    def test_truncated_json_no_closing_bracket(self):
        # Array opened but no ] at all — should recover complete objects
        text = (
            '[{"text":"First.","emotion":"neutral"},{"text":"Second.","emotion":"warm"}'
        )
        chunks = _parse_chunks(text)
        assert len(chunks) == 2
        assert chunks[0].text == "First."
        assert chunks[1].text == "Second."

    def test_truncated_json_partial_object(self):
        # Last object is incomplete — should recover first one only
        text = '[{"text":"Keep this.","emotion":"neutral"},{"text":"Brok'
        chunks = _parse_chunks(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Keep this."

    def test_multiple_chunks_with_pause(self):
        text = """[
            {"text":"Hey.","emotion":"playful","pause_after_ms":300},
            {"text":"","emotion":"neutral","pause_after_ms":500},
            {"text":"Come here.","emotion":"warm","pause_after_ms":0}
        ]"""
        chunks = _parse_chunks(text)
        assert len(chunks) == 3
        assert chunks[0].emotion == "playful"
        assert chunks[1].text == ""
        assert chunks[1].pause_after_ms == 500
        assert chunks[2].text == "Come here."

    def test_pause_only_chunks(self):
        text = '[{"pause_after_ms":500},{"text":"After pause.","emotion":"neutral"}]'
        chunks = _parse_chunks(text)
        assert len(chunks) == 2
        assert chunks[0].text == ""
        assert chunks[0].pause_after_ms == 500
        assert chunks[1].text == "After pause."

    def test_empty_string_returns_empty(self):
        assert _parse_chunks("") == []

    def test_no_json_returns_empty(self):
        assert _parse_chunks("Just plain text, no JSON here.") == []


class TestFallbackPassthrough:
    """_fallback_passthrough: extract quoted dialogue from raw text."""

    def test_double_quoted_dialogue(self):
        text = 'She smiled. "Hello there," she said warmly.'
        chunks = _fallback_passthrough(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hello there,"

    def test_multiple_quotes(self):
        text = '"First." She paused. "Second."'
        chunks = _fallback_passthrough(text)
        assert len(chunks) == 2
        assert chunks[0].text == "First."
        assert chunks[1].text == "Second."

    def test_asterisk_actions_stripped(self):
        text = '*She crosses her arms.* "No way."'
        chunks = _fallback_passthrough(text)
        assert len(chunks) == 1
        assert chunks[0].text == "No way."

    def test_no_quotes_returns_empty(self):
        text = "*She walks across the room.*"
        chunks = _fallback_passthrough(text)
        assert chunks == []

    def test_empty_string(self):
        assert _fallback_passthrough("") == []
