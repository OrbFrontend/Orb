"""Adapter for the built-in Spark-TTS voice cloner."""

from __future__ import annotations

import logging

from ...toolkit import get_settings, spark_voice_speak
from .base import SpeakableChunk, SynthesisResult, TTSAdapter
from .wav import pcm_duration_ms, pcm_to_wav, silence_pcm

logger = logging.getLogger(__name__)

#: Profile voice id for an enrolled voice.
VOICE_ID = "cloned"

_FALLBACK_SAMPLE_RATE = 16000


class BuiltinSparkAdapter(TTSAdapter):
    """Speak through the built-in Spark-TTS cloner."""

    async def synthesize(
        self,
        chunks: list[SpeakableChunk],
        voice_id: str,
        language: str = "en-US",
        rate: float = 1.0,
        pitch: float = 1.0,
        api_url: str = "",
        api_key: str | None = None,
        **kwargs,
    ) -> SynthesisResult:
        """Synthesize each chunk in the enrolled voice and join the clips."""
        text_chunks = [chunk for chunk in chunks if chunk.text.strip()]
        if not text_chunks:
            return SynthesisResult(audio_bytes=b"", content_type="audio/wav")

        # The router passes the enrolled tokens through kwargs.
        speaker_tokens = kwargs.get("speaker_tokens") or []
        if not speaker_tokens:
            raise ValueError("This character has no cloned voice yet — upload a reference clip first.")
        settings = kwargs.get("settings")
        if settings is None:
            settings = await get_settings()

        audio_parts: list[bytes] = []
        sample_rate = _FALLBACK_SAMPLE_RATE
        for index, chunk in enumerate(text_chunks):
            # A leading pause on the first chunk would just delay playback.
            if chunk.pause_before_ms > 0 and index > 0:
                audio_parts.append(silence_pcm(chunk.pause_before_ms, sample_rate))
            pcm, clip_rate = await spark_voice_speak(chunk.text, speaker_tokens, settings)
            if index == 0:  # every clip comes from one model at one rate
                sample_rate = clip_rate
            audio_parts.append(pcm)
            if chunk.pause_after_ms > 0:
                audio_parts.append(silence_pcm(chunk.pause_after_ms, sample_rate))

        raw_pcm = b"".join(audio_parts)
        if not raw_pcm:
            return SynthesisResult(audio_bytes=b"", content_type="audio/wav")
        logger.info("Spark-TTS (built-in): %d chunks → %d bytes at %d Hz", len(text_chunks), len(raw_pcm), sample_rate)
        return SynthesisResult(
            audio_bytes=pcm_to_wav(raw_pcm, sample_rate),
            content_type="audio/wav",
            duration_ms=pcm_duration_ms(raw_pcm, sample_rate),
        )

    async def list_voices(self, language: str = "", **kwargs) -> list[dict]:
        # Enrollment provides the only available voice.
        return []

    @property
    def backend_name(self) -> str:
        return "Spark-TTS (built-in)"
