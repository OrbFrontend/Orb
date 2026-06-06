"""OpenAI-compatible TTS adapter — hits POST /v1/audio/speech.

Works with OpenAI, localai, llama.cpp server, and any endpoint exposing
the /v1/audio/speech interface.

Supports model listing via GET /v1/models.
Voice list is static for OpenAI; compatible endpoints fall back to a generic set.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from .base import SpeakableChunk, SynthesisResult, TTSAdapter

logger = logging.getLogger(__name__)

# OpenAI TTS voices — fixed set
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

# OpenAI-specific TTS model prefixes
_OPENAI_TTS_PREFIXES = ("tts-", "gpt-4o-mini-tts")

_OPENAI_DEFAULT_MODELS = [
    {"id": "tts-1", "name": "tts-1 (standard)", "owned_by": "openai"},
    {"id": "tts-1-hd", "name": "tts-1-hd (high quality)", "owned_by": "openai"},
    {"id": "gpt-4o-mini-tts", "name": "gpt-4o-mini-tts (latest)", "owned_by": "openai"},
]

_DEFAULT_API_URL = "https://api.openai.com"


def _is_openai(api_url: str) -> bool:
    """Check whether the URL points to the official OpenAI API.

    Matches on the parsed host, not a substring, so look-alike URLs such as
    ``https://api.openai.com.evil.com`` or ``https://evil.com/api.openai.com``
    are not mistaken for the official endpoint (which would leak the static
    voice list / TTS-only model filtering to an untrusted host). A missing
    scheme is tolerated by parsing the value as a bare authority.
    """
    parsed = urlparse(api_url if "://" in api_url else f"//{api_url}", scheme="https")
    host = (parsed.hostname or "").lower()
    return host == "api.openai.com" or host.endswith(".api.openai.com")


class OpenAISpeechAdapter(TTSAdapter):
    """OpenAI-compatible TTS adapter using /v1/audio/speech."""

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

        base_url = (api_url or _DEFAULT_API_URL).rstrip("/")
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
            "OpenAI-compatible TTS: %d chars -> %d bytes (voice=%s, model=%s)",
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
        api_url = kwargs.get("api_url", "")
        if _is_openai(api_url):
            return OPENAI_VOICES

        # Compatible endpoint — return a minimal generic set;
        # users can type a custom voice ID in the Voice tab.
        return [
            {"id": "alloy", "name": "alloy (default)", "gender": "neutral"},
        ]

    async def list_models(self, api_url: str = "", api_key: str | None = None, **kwargs) -> list[dict]:
        """Fetch models from /v1/models. Filters for TTS on OpenAI; returns all on compatible endpoints."""
        base_url = (api_url or _DEFAULT_API_URL).rstrip("/")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        openai = _is_openai(base_url)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{base_url}/v1/models", headers=headers)
                resp.raise_for_status()
                data = resp.json()

            models = data.get("data", [])
            if openai:
                # Filter for known TTS model prefixes
                filtered = [
                    {
                        "id": m["id"],
                        "name": m.get("id", ""),
                        "owned_by": m.get("owned_by", ""),
                    }
                    for m in models
                    if any(m["id"].startswith(p) for p in _OPENAI_TTS_PREFIXES)
                ]
                if filtered:
                    return filtered
            else:
                # Compatible endpoint — return all models, let the user pick
                if models:
                    return [
                        {
                            "id": m["id"],
                            "name": m.get("id", ""),
                            "owned_by": m.get("owned_by", ""),
                        }
                        for m in models
                    ]
        except Exception as e:
            logger.debug("Failed to fetch models from %s: %s", base_url, e)

        # Fallback
        return (
            _OPENAI_DEFAULT_MODELS
            if openai
            else [
                {"id": "tts-1", "name": "tts-1 (default)", "owned_by": ""},
            ]
        )

    @property
    def backend_name(self) -> str:
        return "OpenAI-Compatible TTS"

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
