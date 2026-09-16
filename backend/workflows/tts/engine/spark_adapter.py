"""Adapter for the legacy Spark-TTS sidecar backend."""

from __future__ import annotations

import logging

import httpx

from .base import SpeakableChunk, SynthesisResult, TTSAdapter
from .wav import pcm_duration_ms, pcm_to_wav, stitch_pcm, strip_header

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://localhost:9300"
DEFAULT_VOICE = "spark_female_warm"

# Local inference can take several minutes on a cold CPU process.
_SYNTH_TIMEOUT = 300.0
_LIST_TIMEOUT = 10.0


def _base_url(api_url: str) -> str:
    return (api_url or DEFAULT_API_URL).rstrip("/")


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


class SparkTTSAdapter(TTSAdapter):
    """TTS adapter using Spark-TTS-0.5B via a local HTTP server."""

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
        """Synthesize each chunk and join the clips into one WAV."""
        url = f"{_base_url(api_url)}/v1/tts"
        voice = voice_id or DEFAULT_VOICE

        async with httpx.AsyncClient(timeout=_SYNTH_TIMEOUT) as client:

            async def speak(chunk: SpeakableChunk) -> tuple[bytes, int]:
                response = await client.post(
                    url,
                    json={
                        "text": chunk.text,
                        "voice": voice,
                        "speed": rate,
                        "pitch": pitch,
                        "lang": language,
                    },
                    headers=_headers(api_key),
                )
                response.raise_for_status()
                return strip_header(response.content)

            raw_pcm, sample_rate = await stitch_pcm(chunks, speak)
        if not raw_pcm:
            return SynthesisResult(audio_bytes=b"", content_type="audio/wav")

        logger.info(
            "Spark-TTS: %d chunks → %d bytes at %d Hz (voice=%s)",
            sum(bool(chunk.text.strip()) for chunk in chunks),
            len(raw_pcm),
            sample_rate,
            voice,
        )

        return SynthesisResult(
            audio_bytes=pcm_to_wav(raw_pcm, sample_rate),
            content_type="audio/wav",
            duration_ms=pcm_duration_ms(raw_pcm, sample_rate),
        )

    async def list_voices(
        self,
        language: str = "",
        api_url: str = "",
        api_key: str | None = None,
        **kwargs,
    ) -> list[dict]:
        """Fetch presets from the sidecar; empty when it is not running."""
        try:
            async with httpx.AsyncClient(timeout=_LIST_TIMEOUT) as client:
                response = await client.get(
                    f"{_base_url(api_url)}/v1/voices",
                    params={"language": language} if language else None,
                    headers=_headers(api_key),
                )
                response.raise_for_status()
                voices = response.json()
        except Exception as exc:
            # The sidecar is optional, so an unavailable server has no voices.
            logger.debug("could not fetch Spark-TTS voices: %s", exc)
            return []

        if not isinstance(voices, list):
            return []
        return [voice for voice in voices if isinstance(voice, dict) and voice.get("id")]

    @property
    def backend_name(self) -> str:
        return "Spark-TTS (sidecar)"
