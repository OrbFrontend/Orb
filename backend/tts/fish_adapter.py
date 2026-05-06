"""Fish Speech adapter — self-hosted TTS via native HTTP API.

Fish Speech (https://github.com/fishaudio/fish-speech) uses its own API:
- POST /v1/tts          — synthesize speech
- GET  /v1/references/list — list voice references (saved speaker profiles)

Request format:
    POST /v1/tts
    {"text": "...", "reference_id": "...", "format": "mp3", "temperature": 0.8}

Output: wav/mp3/opus/pcm binary audio.
"""

from __future__ import annotations

import logging

import httpx

from .base import SpeakableChunk, SynthesisResult, TTSAdapter

logger = logging.getLogger(__name__)


class FishSpeechAdapter(TTSAdapter):
    """TTS adapter for Fish Speech's native API server."""

    async def synthesize(
        self,
        chunks: list[SpeakableChunk],
        voice_id: str,
        language: str = "en-US",
        rate: float = 1.0,
        pitch: float = 1.0,
        api_url: str = "",
        api_key: str | None = None,
        model: str = "",
        **kwargs,
    ) -> SynthesisResult:
        text = self._chunks_to_text(chunks)
        if not text.strip():
            return SynthesisResult(audio_bytes=b"", content_type="audio/mpeg")

        base_url = (api_url or "http://localhost:8080").rstrip("/")
        url = f"{base_url}/v1/tts"

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body = {
            "text": text,
            "format": "mp3",
            "normalize": True,
        }

        # voice_id = Fish Speech reference_id
        if voice_id and voice_id != "default":
            body["reference_id"] = voice_id

        # Map rate to temperature (loose mapping — faster = lower temp)
        temp = max(0.1, min(1.0, 0.8 / max(rate, 0.1)))
        body["temperature"] = round(temp, 2)

        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()

        logger.info(
            "Fish TTS: %d chars -> %d bytes (ref=%s)",
            len(text),
            len(resp.content),
            voice_id or "default",
        )

        return SynthesisResult(
            audio_bytes=resp.content,
            content_type="audio/mpeg",
        )

    async def list_voices(
        self,
        language: str = "",
        api_url: str = "",
        api_key: str | None = None,
        **kwargs,
    ) -> list[dict]:
        """Fetch voice references from /v1/references/list."""
        base_url = (api_url or "http://localhost:8080").rstrip("/")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{base_url}/v1/references/list", headers=headers
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Fish returns references with id, name, etc.
                    refs = (
                        data if isinstance(data, list) else data.get("references", [])
                    )
                    return [
                        {
                            "id": r.get("id", r.get("reference_id", "")),
                            "name": r.get("name", r.get("id", "unknown")),
                            "gender": r.get("gender", "unknown"),
                            "language": r.get("language", ""),
                        }
                        for r in refs
                    ]
        except Exception as e:
            logger.debug("Failed to fetch Fish Speech references: %s", e)

        # Fallback
        return [{"id": "default", "name": "Default", "gender": "unknown"}]

    async def list_models(
        self, api_url: str = "", api_key: str | None = None, **kwargs
    ) -> list[dict]:
        """Fish Speech doesn't have a model list endpoint."""
        return []

    @property
    def backend_name(self) -> str:
        return "Fish Speech"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_emotion_tags(self) -> bool:
        return False
