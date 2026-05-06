from __future__ import annotations

import pytest

from backend.tts.base import SpeakableChunk
from backend.tts.elevenlabs_adapter import ElevenLabsAdapter


class FakeResponse:
    content = b"mp3"

    def raise_for_status(self):
        return None


class FakeAsyncClient:
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, json, headers):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return FakeResponse()


@pytest.mark.asyncio
async def test_elevenlabs_uses_profile_model_alias(monkeypatch):
    import backend.tts.elevenlabs_adapter as adapter_module

    FakeAsyncClient.requests.clear()
    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", FakeAsyncClient)

    result = await ElevenLabsAdapter().synthesize(
        chunks=[SpeakableChunk(text="Hello.")],
        voice_id="voice-1",
        api_key="test-key",
        model_id="eleven_turbo_v2_5",
    )

    assert result.audio_bytes == b"mp3"
    assert FakeAsyncClient.requests[0]["json"]["model_id"] == "eleven_turbo_v2_5"
