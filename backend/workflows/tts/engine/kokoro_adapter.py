"""
backend/tts/kokoro_adapter.py — Kokoro-82M TTS backend adapter.

Calls a local Kokoro TTS API server (Python 3.11 required, kokoro won't install on 3.13+).
The server runs as a separate process at http://localhost:9200 by default.

Model: https://github.com/hexgrad/kokoro
"""

from __future__ import annotations

import io
import logging
import wave

import httpx

from .base import SpeakableChunk, SynthesisResult, TTSAdapter

logger = logging.getLogger(__name__)

# Kokoro voice prefix -> language code mapping
# af/am = American English, bf/bm = British English, etc.
_VOICE_LANG_MAP = {
    "a": "en-US",  # American English
    "b": "en-GB",  # British English
    "e": "es-ES",  # Spanish
    "f": "fr-FR",  # French
    "h": "hi-IN",  # Hindi
    "i": "it-IT",  # Italian
    "j": "ja-JP",  # Japanese
    "p": "pt-BR",  # Brazilian Portuguese
    "z": "zh-CN",  # Chinese
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
        """Synthesize chunks into WAV audio via Kokoro API server.

        Synthesizes each text chunk individually and concatenates the audio
        with real silence padding between them (based on pause_before/after_ms).
        This gives precise pause timing instead of relying on punctuation tricks.
        """
        text_chunks = [c for c in chunks if c.text.strip()]
        if not text_chunks:
            return SynthesisResult(audio_bytes=b"", content_type="audio/wav")

        base_url = (api_url or "http://localhost:9200").rstrip("/")
        url = f"{base_url}/v1/tts"

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        lang_short = language.split("-")[0].lower() if language else "en"
        kokoro_lang = _LANG_TO_KOKORO.get(lang_short, "a")

        # Synthesize each chunk and collect raw PCM data
        audio_parts: list[bytes] = []
        sample_rate = 24000

        async with httpx.AsyncClient(timeout=60.0) as client:
            for i, chunk in enumerate(text_chunks):
                # Prepend silence for pause_before_ms
                if chunk.pause_before_ms > 0 and i > 0:
                    audio_parts.append(_silence_pcm(chunk.pause_before_ms, sample_rate))

                body = {
                    "text": chunk.text,
                    "voice": voice_id or "af_heart",
                    "speed": rate,
                    "lang": kokoro_lang,
                }
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()

                # Strip WAV header, keep raw PCM
                audio_parts.append(_wav_strip_header(resp.content))

                # Append silence for pause_after_ms
                if chunk.pause_after_ms > 0:
                    audio_parts.append(_silence_pcm(chunk.pause_after_ms, sample_rate))

        if not audio_parts or all(p == b"" for p in audio_parts):
            return SynthesisResult(audio_bytes=b"", content_type="audio/wav")

        # Wrap concatenated PCM in a WAV container
        raw_pcm = b"".join(audio_parts)
        wav_bytes = _pcm_to_wav(raw_pcm, sample_rate)

        logger.info(
            "Kokoro synthesis: %d chunks, %d bytes, voice=%s",
            len(text_chunks),
            len(wav_bytes),
            voice_id or "af_heart",
        )

        return SynthesisResult(
            audio_bytes=wav_bytes,
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


# ---------------------------------------------------------------------------
# WAV helpers for per-chunk concatenation
# ---------------------------------------------------------------------------


def _silence_pcm(duration_ms: int, sample_rate: int = 24000) -> bytes:
    """Generate silent PCM frames (16-bit mono)."""
    n_samples = int(sample_rate * duration_ms / 1000)
    return b"\x00\x00" * n_samples


def _wav_strip_header(wav_bytes: bytes) -> bytes:
    """Extract raw PCM data from a WAV byte string."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return w.readframes(w.getnframes())


def _pcm_to_wav(raw_pcm: bytes, sample_rate: int = 24000) -> bytes:
    """Wrap raw 16-bit mono PCM bytes in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(raw_pcm)
    return buf.getvalue()


# Map language codes to Kokoro lang_code (single char)
_LANG_TO_KOKORO = {
    "en": "a",
    "us": "a",
    "gb": "b",
    "uk": "b",
    "es": "e",
    "fr": "f",
    "hi": "h",
    "it": "i",
    "ja": "j",
    "jp": "j",
    "pt": "p",
    "br": "p",
    "zh": "z",
    "cn": "z",
}


# Default voice list (used when server is not reachable). Derive display fields
# from Kokoro's two-letter voice prefix so the fallback cannot drift internally.
_DEFAULT_VOICES = [
    {
        "id": voice_id,
        "name": f"{voice_id.split('_', 1)[1].title()} ({'American' if voice_id[0] == 'a' else 'British'} {_GENDER_MAP[voice_id[1]]})",
        "language": _VOICE_LANG_MAP[voice_id[0]],
        "gender": _GENDER_MAP[voice_id[1]],
    }
    for voice_id in (
        "af_heart",
        "af_bella",
        "af_nicole",
        "af_sarah",
        "af_nova",
        "af_river",
        "af_sky",
        "am_adam",
        "am_michael",
        "am_liam",
        "bf_emma",
        "bf_alice",
        "bm_george",
        "bm_daniel",
    )
]
