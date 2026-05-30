"""Unit tests for the Edge TTS adapter.

Tests helper functions without hitting the Edge TTS API.
"""

from __future__ import annotations

from backend.workflows.tts.engine.base import SpeakableChunk
from backend.workflows.tts.engine.edge_adapter import EdgeTTSAdapter, _format_pitch, _format_rate


class TestFormatRate:
    """_format_rate: float → edge-tts rate string."""

    def test_normal_speed(self):
        assert _format_rate(1.0) == "+0%"

    def test_faster(self):
        assert _format_rate(1.5) == "+50%"

    def test_slower(self):
        assert _format_rate(0.7) == "-30%"


class TestFormatPitch:
    """_format_pitch: float → edge-tts pitch string."""

    def test_normal_pitch(self):
        assert _format_pitch(1.0) == "+0Hz"

    def test_higher(self):
        assert _format_pitch(1.5) == "+25Hz"

    def test_lower(self):
        assert _format_pitch(0.5) == "-25Hz"


class TestChunksToText:
    """EdgeTTSAdapter._chunks_to_text joins chunks into speakable text."""

    def setup_method(self):
        self.adapter = EdgeTTSAdapter()

    def test_single_chunk(self):
        chunks = [SpeakableChunk(text="Hello.", emotion="neutral")]
        text = self.adapter._chunks_to_text(chunks)
        assert "Hello." in text

    def test_multiple_chunks_joined(self):
        chunks = [
            SpeakableChunk(text="First.", emotion="neutral", pause_after_ms=500),
            SpeakableChunk(text="Second.", emotion="neutral"),
        ]
        text = self.adapter._chunks_to_text(chunks)
        assert "First." in text
        assert "Second." in text

    def test_pause_only_chunk(self):
        chunks = [
            SpeakableChunk(text="", emotion="neutral", pause_after_ms=1000),
            SpeakableChunk(text="After pause.", emotion="neutral"),
        ]
        text = self.adapter._chunks_to_text(chunks)
        assert "After pause." in text
