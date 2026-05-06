"""OpenAI TTS adapter — hits POST /v1/audio/speech.

Supports model listing via GET /v1/models (filters for tts-* models).
Voice list is static (OpenAI provides a fixed set).
"""

from __future__ import annotations

import logging

import httpx

from .base import SpeakableChunk, SynthesisResult, TTSAdapter

logger = logging.getLogger(__name__)

# OpenAI TTS voices — fixed set, unlikely to change
OPENAI_VOICES = [
    {"id": "alloy", "name": "Alloy", "gender": "neutral"},
    {"id": "ash", "name": "Ash", "gender": "neutral"},
    {"id": "ballad", "name": "Ballad", "gender": "neutral"},
    {"id": "coral", "name": "Coral", "gender": "neutral"},
    {"id": "echo", "name": "Echo", "gender": "male"},
    {"id": "fable", "name": "Fable", "gender": "neutral"},
    {"id": "onyx", "name": "Onyx", "gender": "male"},
    {"id": "nova", "name": "Nova", "gender": "female"},
    {"id": "sage", "name": "Sage", "gender": "neutral"},
    {"id": "shimmer", "name": "Shimmer", "gender": "female"},
]

# Known TTS model prefixes to filter from /v1/models
TTS_MODEL_PREFIXES = ("tts-", "gpt-4o-mini-tts")


class OpenAISpeechAdapter(TTSAdapter):
    """OpenAI native TTS adapter using /v1/audio/speech."""

    async def synthesize(
        self,
        chunks: list[SpeakableChunk],
        voice_id: str,
        language: str = "en-US",
        rate: float = 1.0,
        pitch: float = 1.0,
        api_url: str = "",
        api_key: str | None = None,
        model: str = "tts-1",
        response_format: str = "mp3",
        **kwargs,
    ) -> SynthesisResult:
        text = self._chunks_to_text(chunks)
        if not text.strip():
            return SynthesisResult(audio_bytes=b"", content_type="audio/mpeg")

        base_url = (api_url or "https://api.openai.com").rstrip("/")
        url = f"{base_url}/v1/audio/speech"

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body = {
            "model": model or "tts-1",
            "input": text,
            "voice": voice_id or "alloy",
            "response_format": response_format,
            "speed": rate,
        }

        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()

        content_type = _format_to_mime(response_format)

        logger.info(
            "OpenAI TTS: %d chars -> %d bytes (voice=%s, model=%s)",
            len(text),
            len(resp.content),
            voice_id,
            model,
        )

        return SynthesisResult(
            audio_bytes=resp.content,
            content_type=content_type,
        )

    async def list_voices(self, language: str = "", **kwargs) -> list[dict]:
        return OPENAI_VOICES

    async def list_models(
        self, api_url: str = "", api_key: str | None = None, **kwargs
    ) -> list[dict]:
        """Fetch models from /v1/models, filter for TTS-capable ones."""
        base_url = (api_url or "https://api.openai.com").rstrip("/")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{base_url}/v1/models", headers=headers)
                resp.raise_for_status()
                data = resp.json()

            models = data.get("data", [])
            # Filter for TTS models
            tts_models = [
                {
                    "id": m["id"],
                    "name": m.get("id", ""),
                    "owned_by": m.get("owned_by", ""),
                }
                for m in models
                if any(m["id"].startswith(p) for p in TTS_MODEL_PREFIXES)
            ]
            if tts_models:
                return tts_models
        except Exception as e:
            logger.debug("Failed to fetch models from %s: %s", base_url, e)

        # Fallback defaults
        return [
            {"id": "tts-1", "name": "tts-1 (standard)", "owned_by": "openai"},
            {"id": "tts-1-hd", "name": "tts-1-hd (high quality)", "owned_by": "openai"},
            {
                "id": "gpt-4o-mini-tts",
                "name": "gpt-4o-mini-tts (latest)",
                "owned_by": "openai",
            },
        ]

    @property
    def backend_name(self) -> str:
        return "OpenAI TTS (and compatible)"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_emotion_tags(self) -> bool:
        return False


def _format_to_mime(fmt: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "opus": "audio/opus",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "pcm": "audio/pcm",
    }.get(fmt, "audio/mpeg")
