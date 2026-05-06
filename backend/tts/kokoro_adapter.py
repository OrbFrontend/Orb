"""
backend/tts/kokoro_adapter.py — Kokoro-82M TTS backend adapter.

Calls a local Kokoro TTS API server (Python 3.11 required, kokoro won't install on 3.13+).
The server runs as a separate process at http://localhost:9200 by default.

Server repo: ~/repos/kokoro-tts/
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import httpx

from .base import AudioChunk, SpeakableChunk, SynthesisResult, TTSAdapter

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Kokoro voice prefix -> language code mapping
# af/am = American English, bf/bm = British English, etc.
_VOICE_LANG_MAP = {
    "a": "en-US",   # American English
    "b": "en-GB",   # British English
    "e": "es-ES",   # Spanish
    "f": "fr-FR",   # French
    "h": "hi-IN",   # Hindi
    "i": "it-IT",   # Italian
    "j": "ja-JP",   # Japanese
    "p": "pt-BR",   # Brazilian Portuguese
    "z": "zh-CN",   # Chinese
}

_GENDER_MAP = {
    "f": "Female",
    "m": "Male",
}


class KokoroTTSAdapter(TTSAdapter):
    """TTS adapter using Kokoro-82M via local HTTP API server."""

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
        """Synthesize chunks into WAV audio via Kokoro API server."""
        text = self._chunks_to_text(chunks)

        if not text.strip():
            return SynthesisResult(audio_bytes=b"", content_type="audio/wav")

        base_url = (api_url or "http://localhost:9200").rstrip("/")
        url = f"{base_url}/v1/tts"

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Map language codes to Kokoro lang_code (single char)
        _LANG_TO_KOKORO = {
            "en": "a", "us": "a",
            "gb": "b", "uk": "b",
            "es": "e", "fr": "f", "hi": "h",
            "it": "i", "ja": "j", "jp": "j",
            "pt": "p", "br": "p",
            "zh": "z", "cn": "z",
        }
        lang_short = language.split("-")[0].lower() if language else "en"
        kokoro_lang = _LANG_TO_KOKORO.get(lang_short, "a")

        body = {
            "text": text,
            "voice": voice_id or "af_heart",
            "speed": rate,
            "lang": kokoro_lang,
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()

        logger.info(
            "Kokoro synthesis: %d bytes, voice=%s",
            len(resp.content),
            voice_id or "af_heart",
        )

        return SynthesisResult(
            audio_bytes=resp.content,
            content_type="audio/wav",
        )

    async def list_voices(self, language: str = "", **kwargs) -> list[dict]:
        """Fetch available Kokoro voices from the API server."""
        api_url = kwargs.get("api_url", "")
        base_url = (api_url or "http://localhost:9200").rstrip("/")

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/v1/voices")
                resp.raise_for_status()
                voices = resp.json()
        except Exception:
            # Fallback: return common voices without server call
            voices = _DEFAULT_VOICES

        if language:
            lang_prefix = language.split("-")[0].lower()
            voices = [v for v in voices if v.get("language", "").startswith(lang_prefix)]

        return voices

    @property
    def backend_name(self) -> str:
        return "Kokoro-82M"

    @property
    def supports_streaming(self) -> bool:
        return False


# Default voice list (used when server is not reachable)
_DEFAULT_VOICES = [
    {"id": "af_heart", "name": "Heart (American Female)", "language": "en-US", "gender": "Female"},
    {"id": "af_bella", "name": "Bella (American Female)", "language": "en-US", "gender": "Female"},
    {"id": "af_nicole", "name": "Nicole (American Female)", "language": "en-US", "gender": "Female"},
    {"id": "af_sarah", "name": "Sarah (American Female)", "language": "en-US", "gender": "Female"},
    {"id": "af_nova", "name": "Nova (American Female)", "language": "en-US", "gender": "Female"},
    {"id": "af_river", "name": "River (American Female)", "language": "en-US", "gender": "Female"},
    {"id": "af_sky", "name": "Sky (American Female)", "language": "en-US", "gender": "Female"},
    {"id": "am_adam", "name": "Adam (American Male)", "language": "en-US", "gender": "Male"},
    {"id": "am_michael", "name": "Michael (American Male)", "language": "en-US", "gender": "Male"},
    {"id": "am_liam", "name": "Liam (American Male)", "language": "en-US", "gender": "Male"},
    {"id": "bf_emma", "name": "Emma (British Female)", "language": "en-GB", "gender": "Female"},
    {"id": "bf_alice", "name": "Alice (British Female)", "language": "en-GB", "gender": "Female"},
    {"id": "bm_george", "name": "George (British Male)", "language": "en-GB", "gender": "Male"},
    {"id": "bm_daniel", "name": "Daniel (British Male)", "language": "en-GB", "gender": "Male"},
]
