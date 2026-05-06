"""ElevenLabs TTS adapter — cloud API.

Requires an API key (passed via voice profile or endpoint config).
Supports 300+ voices, voice cloning, emotion control, streaming.
Output: MP3.
"""

from __future__ import annotations

import logging

import httpx

from .base import SpeakableChunk, SynthesisResult, TTSAdapter

logger = logging.getLogger(__name__)

_API_BASE = "https://api.elevenlabs.io"


class ElevenLabsAdapter(TTSAdapter):
    """TTS adapter using the ElevenLabs cloud API."""

    async def synthesize(
        self,
        chunks: list[SpeakableChunk],
        voice_id: str,
        language: str = "en-US",
        rate: float = 1.0,
        pitch: float = 1.0,
        api_key: str | None = None,
        model_id: str = "eleven_multilingual_v2",
        **kwargs,
    ) -> SynthesisResult:
        if not api_key:
            raise ValueError("ElevenLabs requires an API key")

        text = self._chunks_to_text(chunks)
        if not text.strip():
            return SynthesisResult(audio_bytes=b"", content_type="audio/mpeg")

        url = f"{_API_BASE}/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        body = {
            "text": text,
            "model_id": model_id,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        }

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()

        logger.info(
            "ElevenLabs: synthesized %d chars → %d bytes (voice=%s)",
            len(text),
            len(resp.content),
            voice_id,
        )

        return SynthesisResult(
            audio_bytes=resp.content,
            content_type="audio/mpeg",
        )

    async def list_voices(
        self, language: str = "", api_key: str | None = None, **kwargs
    ) -> list[dict]:
        if not api_key:
            return []

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_API_BASE}/v1/voices",
                headers={"xi-api-key": api_key},
            )
            resp.raise_for_status()

        data = resp.json()
        voices = []
        for v in data.get("voices", []):
            labels = v.get("labels", {})
            voice_lang = labels.get("language", "en")
            if language and not voice_lang.startswith(language.split("-")[0]):
                continue
            voices.append(
                {
                    "id": v["voice_id"],
                    "name": v["name"],
                    "language": voice_lang,
                    "gender": labels.get("gender", "unknown"),
                }
            )
        return voices

    @property
    def backend_name(self) -> str:
        return "ElevenLabs"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_emotion_tags(self) -> bool:
        return True
