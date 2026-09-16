"""Built-in Spark-TTS adapter."""

from __future__ import annotations

import logging

from ...toolkit import get_settings, spark_voice_speak
from .base import SpeakableChunk, SynthesisResult, TTSAdapter
from .wav import pcm_duration_ms, pcm_to_wav, stitch_pcm

logger = logging.getLogger(__name__)

VOICE_ID = "cloned"


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
        speaker_tokens = kwargs.get("speaker_tokens") or []
        settings = kwargs.get("settings")

        async def speak(chunk: SpeakableChunk) -> tuple[bytes, int]:
            nonlocal settings
            if not speaker_tokens:
                raise ValueError("This character has no cloned voice yet — upload a reference clip first.")
            if settings is None:
                settings = await get_settings()
            return await spark_voice_speak(chunk.text, speaker_tokens, settings)

        raw_pcm, sample_rate = await stitch_pcm(chunks, speak)
        if not raw_pcm:
            return SynthesisResult(audio_bytes=b"", content_type="audio/wav")
        logger.info(
            "Spark-TTS (built-in): %d chunks → %d bytes at %d Hz",
            sum(bool(chunk.text.strip()) for chunk in chunks),
            len(raw_pcm),
            sample_rate,
        )
        return SynthesisResult(
            audio_bytes=pcm_to_wav(raw_pcm, sample_rate),
            content_type="audio/wav",
            duration_ms=pcm_duration_ms(raw_pcm, sample_rate),
        )

    async def list_voices(self, language: str = "", **kwargs) -> list[dict]:
        return []

    @property
    def backend_name(self) -> str:
        return "Spark-TTS (built-in)"
