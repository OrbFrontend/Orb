"""
backend/tts/base.py — Abstract base class for TTS adapters.

All TTS backends implement this interface. The Speech Scripter produces
SpeakableChunks, which adapters translate into audio via their backend-specific API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SpeakableChunk:
    """A unit of text ready for TTS synthesis.

    The Speech Scripter produces these from writer output.
    Each chunk has its own emotion/prosody settings.
    """

    text: str
    emotion: str = "neutral"
    pause_before_ms: int = 0
    pause_after_ms: int = 0
    voice_hint: str = ""  # For multi-character voice switching (Phase 3)

    # Valid emotions (shared across all backends)
    EMOTIONS = frozenset(
        {
            "neutral",
            "warm",
            "soft",
            "playful",
            "teasing",
            "sad",
            "angry",
            "fearful",
            "surprised",
            "whispered",
            "breathless",
            "amused",
        }
    )


@dataclass
class AudioChunk:
    """A chunk of synthesized audio."""

    audio_bytes: bytes
    sequence: int
    final: bool = False


@dataclass
class SynthesisResult:
    """Complete synthesis output from a TTS backend."""

    audio_bytes: bytes  # Complete audio data (MP3/WAV)
    content_type: str = "audio/mpeg"  # MIME type
    duration_ms: int = 0  # Estimated duration (0 if unknown)
    size_bytes: int = 0

    def __post_init__(self):
        if not self.size_bytes:
            self.size_bytes = len(self.audio_bytes)


class TTSAdapter(ABC):
    """Abstract base class for TTS backends.

    Each adapter wraps a specific TTS service (Edge TTS, Fish Speech, etc.)
    and translates SpeakableChunks into audio.
    """

    @abstractmethod
    async def synthesize(
        self,
        chunks: list[SpeakableChunk],
        voice_id: str,
        language: str = "en-US",
        rate: float = 1.0,
        pitch: float = 1.0,
        **kwargs: Any,
    ) -> SynthesisResult:
        """Synthesize speakable chunks into complete audio.

        Args:
            chunks: Speakable text segments with emotion/prosody hints.
            voice_id: Backend-specific voice identifier.
            language: Language code (e.g. 'en-US').
            rate: Speech rate multiplier (1.0 = normal).
            pitch: Pitch multiplier (1.0 = normal).

        Returns:
            SynthesisResult with complete audio bytes.
        """
        ...

    @abstractmethod
    async def list_voices(self, language: str = "", **kwargs: Any) -> list[dict]:
        """Return available voices for this backend.

        Args:
            language: Optional language filter (e.g. 'en').

        Returns:
            List of dicts with keys: id, name, language, gender.
        """
        ...

    async def list_models(self, **kwargs: Any) -> list[dict]:
        """Return available models for backends that expose model selection."""
        return []

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Human-readable backend name."""
        ...

    @property
    def supports_streaming(self) -> bool:
        """Whether this backend supports chunk-by-chunk streaming."""
        return False

    @property
    def supports_emotion_tags(self) -> bool:
        """Whether this backend supports inline emotion tags like [laugh]."""
        return False

    def _chunks_to_text(self, chunks: list[SpeakableChunk]) -> str:
        """Merge chunks into plain text with natural pauses.

        Used by backends that don't support explicit pause markers.
        Punctuation-based pauses: periods, ellipses, commas.
        """
        parts = []
        for chunk in chunks:
            if chunk.pause_before_ms >= 500:
                parts.append("...")
            elif chunk.pause_before_ms >= 200:
                parts.append(".")
            parts.append(chunk.text)
            if chunk.pause_after_ms >= 500:
                parts.append("...")
            elif chunk.pause_after_ms >= 200:
                parts.append(".")
        return " ".join(parts)
