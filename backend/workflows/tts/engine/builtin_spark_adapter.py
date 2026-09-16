"""Built-in Spark-TTS voice cloner — no sidecar, no torch, no voices.json.

The user-facing workflow is one step: upload an audio file to a character, and
that character speaks in that voice from then on. What is stored is 32 integers
in the character's voice profile; the reference clip itself is never consulted
again at synthesis time.

Differences from the sidecar adapter this replaces (``spark_adapter``, still
registered as ``spark_remote`` for existing profiles):

- There is no ``api_url``. The model runs in-process, through the same
  llama-server runtime the Prose Rewriter uses.
- ``list_voices`` returns the character's own enrolled voice, or nothing, and
  its failure mode is a reason rather than silence.
- ``rate``/``pitch`` DO NOTHING and the panel greys them out. That is not an
  omission: Spark-TTS takes prosody attributes only on its control path, and
  cloning bypasses that path entirely. Sending values that are silently ignored
  is worse than not offering them.
"""

from __future__ import annotations

import logging

from ...toolkit import get_settings, spark_voice_ready, spark_voice_speak
from .base import SpeakableChunk, SynthesisResult, TTSAdapter
from .wav import pcm_duration_ms, pcm_to_wav, silence_pcm

logger = logging.getLogger(__name__)

#: The profile field an enrolled voice lives in, and the id the picker shows.
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

        # The 32 ints ride on the profile, so they arrive here through the same
        # **kwargs the router already forwards. Validated by the caller
        # (normalize_profile) and again by the token layer before the codec.
        speaker_tokens = kwargs.get("speaker_tokens") or []
        if not speaker_tokens:
            raise ValueError("This character has no cloned voice yet — upload a reference clip first.")
        # Resolved once per call, and only here: a preview or a reroll arrives
        # with no settings, and the Local ML toggles are not optional to read.
        settings = kwargs.get("settings")
        if settings is None:
            settings = await get_settings()
        ok, reason = spark_voice_ready(settings)
        if not ok:
            raise ValueError(reason)

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
        """The one voice this backend has: the character's own, once enrolled.

        There is no catalog to fetch and nothing to reload, so this answers from
        the form's own state. An unenrolled character gets an empty list, which
        the panel renders as the upload control rather than a broken picker.
        """
        if not kwargs.get("speaker_tokens"):
            return []
        name = str(kwargs.get("speaker_ref_name") or "").strip()
        return [
            {
                "id": VOICE_ID,
                "name": f"Cloned voice ({name})" if name else "Cloned voice",
                "language": language or "",
                "gender": "",
            }
        ]

    @property
    def backend_name(self) -> str:
        return "Spark-TTS (built-in)"
