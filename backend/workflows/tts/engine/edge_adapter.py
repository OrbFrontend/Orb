"""
backend/tts/edge_adapter.py — Edge TTS backend adapter.

Uses the edge-tts library (Microsoft Edge's online TTS service).
Free, no API key required, 400+ voices, outputs MP3.

Limitations:
- No SSML support in user text (text is XML-escaped internally)
- Pauses via punctuation only (periods, ellipses, commas)
- Unofficial API — could break if Microsoft changes their endpoint
- stream() can only be called once per Communicate instance
"""

from __future__ import annotations

import logging

import edge_tts

from .base import TTSAdapter, SpeakableChunk, SynthesisResult

logger = logging.getLogger(__name__)

# Default voice for English female
DEFAULT_VOICE = "en-US-JennyNeural"

# edge-tts reports word offsets and durations in 100ns ticks; 10,000 ticks == 1ms.
_TICKS_PER_MS = 10_000


def _format_rate(rate: float) -> str:
    """Convert float rate (1.0 = normal) to edge-tts rate string."""
    pct = int((rate - 1.0) * 100)
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


def _format_pitch(pitch: float) -> str:
    """Convert float pitch (1.0 = normal) to edge-tts pitch string."""
    hz = int((pitch - 1.0) * 50)
    return f"+{hz}Hz" if hz >= 0 else f"{hz}Hz"


class EdgeTTSAdapter(TTSAdapter):
    """TTS adapter using Microsoft Edge's online TTS service via edge-tts."""

    async def synthesize(
        self,
        chunks: list[SpeakableChunk],
        voice_id: str,
        language: str = "en-US",
        rate: float = 1.0,
        pitch: float = 1.0,
        **kwargs,
    ) -> SynthesisResult:
        """Synthesize chunks into a complete MP3 file.

        Merges chunks into plain text with punctuation-based pauses,
        then sends to Edge TTS and buffers the complete MP3 output.
        """
        text = self._chunks_to_text(chunks)

        if not text.strip():
            return SynthesisResult(audio_bytes=b"", content_type="audio/mpeg")

        # boundary defaults to SentenceBoundary; request word-level events so the
        # workflow can align per-word timings. Guarded for an older edge-tts that
        # lacks the kwarg -- without word events the caller estimates timing.
        try:
            communicate = edge_tts.Communicate(
                text,
                voice_id or DEFAULT_VOICE,
                rate=_format_rate(rate),
                pitch=_format_pitch(pitch),
                boundary="WordBoundary",
            )
        except TypeError:
            communicate = edge_tts.Communicate(
                text,
                voice_id or DEFAULT_VOICE,
                rate=_format_rate(rate),
                pitch=_format_pitch(pitch),
            )

        audio_parts = []
        boundaries: list[dict] = []
        async for chunk in communicate.stream():
            ctype = chunk.get("type")
            if ctype == "audio":
                data = chunk.get("data")
                if isinstance(data, bytes):
                    audio_parts.append(data)
            elif ctype == "WordBoundary":
                offset = chunk.get("offset") or 0
                duration = chunk.get("duration") or 0
                boundaries.append(
                    {
                        "text": chunk.get("text") or "",
                        "start_ms": offset // _TICKS_PER_MS,
                        "end_ms": (offset + duration) // _TICKS_PER_MS,
                    }
                )

        audio_bytes = b"".join(audio_parts)
        logger.info(
            "Edge TTS: synthesized %d chars → %d bytes MP3 (voice=%s)",
            len(text),
            len(audio_bytes),
            voice_id,
        )

        return SynthesisResult(
            audio_bytes=audio_bytes,
            content_type="audio/mpeg",
            word_boundaries=boundaries or None,
        )

    async def list_voices(self, language: str = "", **kwargs) -> list[dict]:
        """Return available Edge TTS voices, optionally filtered by language."""
        voices = await edge_tts.list_voices()
        if language:
            lang_prefix = language.split("-")[0]  # "en-US" → "en"
            voices = [v for v in voices if v["Locale"].startswith(lang_prefix)]
        return [
            {
                "id": v["ShortName"],
                "name": v["FriendlyName"],
                "language": v["Locale"],
                "gender": v["Gender"],
            }
            for v in voices
        ]

    @property
    def backend_name(self) -> str:
        return "Microsoft Edge TTS"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_emotion_tags(self) -> bool:
        return False
