"""Unit tests for the Spark-TTS adapter.

Exercises chunk stitching and payload shape against a fake sidecar.
"""

from __future__ import annotations

import io
import json
import wave

import pytest

from backend.workflows.tts.engine.base import SpeakableChunk
from backend.workflows.tts.engine.spark_adapter import SparkTTSAdapter
from backend.workflows.tts.engine.wav import pcm_to_wav, strip_header

SAMPLE_RATE = 16000


def clip(frames: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """A WAV clip of ``frames`` non-silent samples."""
    return pcm_to_wav(b"\x01\x00" * frames, sample_rate)


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        return None

    def json(self):
        return json.loads(self.content)


class FakeAsyncClient:
    """Records requests and replays a queued response per call."""

    requests: list[dict] = []
    post_responses: list[bytes] = []
    get_response: bytes | None = None
    get_error: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, json=None, headers=None):
        type(self).requests.append({"url": url, "json": json, "headers": headers})
        index = len(type(self).requests) - 1
        queued = type(self).post_responses
        return FakeResponse(queued[index] if index < len(queued) else queued[-1])

    async def get(self, url, params=None, headers=None):
        type(self).requests.append({"url": url, "params": params, "headers": headers})
        if type(self).get_error:
            raise type(self).get_error
        return FakeResponse(type(self).get_response or b"[]")


@pytest.fixture
def fake_client(monkeypatch):
    import backend.workflows.tts.engine.spark_adapter as adapter_module

    FakeAsyncClient.requests = []
    FakeAsyncClient.post_responses = [clip(100)]
    FakeAsyncClient.get_response = None
    FakeAsyncClient.get_error = None
    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


class TestSynthesize:
    @pytest.mark.asyncio
    async def test_sends_one_request_per_chunk_with_prosody(self, fake_client):
        await SparkTTSAdapter().synthesize(
            chunks=[SpeakableChunk(text="One."), SpeakableChunk(text="Two.")],
            voice_id="spark_male_deep",
            language="en-US",
            rate=1.25,
            pitch=0.8,
            api_url="http://localhost:9300/",
        )

        assert len(fake_client.requests) == 2
        first = fake_client.requests[0]
        # Trailing slash in the configured URL must not double up.
        assert first["url"] == "http://localhost:9300/v1/tts"
        assert first["json"] == {
            "text": "One.",
            "voice": "spark_male_deep",
            "speed": 1.25,
            "pitch": 0.8,
            "lang": "en-US",
        }

    @pytest.mark.asyncio
    async def test_pauses_become_real_silence_between_clips(self, fake_client):
        fake_client.post_responses = [clip(100), clip(100)]

        result = await SparkTTSAdapter().synthesize(
            chunks=[
                SpeakableChunk(text="One.", pause_after_ms=500),
                SpeakableChunk(text="Two.", pause_before_ms=500),
            ],
            voice_id="spark_female_warm",
        )

        pcm, rate = strip_header(result.audio_bytes)
        assert rate == SAMPLE_RATE
        # 200 speech frames plus two 500 ms gaps at 16 kHz.
        assert len(pcm) == (200 + 2 * 8000) * 2

    @pytest.mark.asyncio
    async def test_leading_pause_on_first_chunk_is_dropped(self, fake_client):
        result = await SparkTTSAdapter().synthesize(
            chunks=[SpeakableChunk(text="One.", pause_before_ms=1000)],
            voice_id="spark_female_warm",
        )

        pcm, _ = strip_header(result.audio_bytes)
        assert len(pcm) == 100 * 2

    @pytest.mark.asyncio
    async def test_sample_rate_follows_the_sidecar(self, fake_client):
        fake_client.post_responses = [clip(240, sample_rate=24000)]

        result = await SparkTTSAdapter().synthesize(
            chunks=[SpeakableChunk(text="One.")],
            voice_id="spark_female_warm",
        )

        with wave.open(io.BytesIO(result.audio_bytes), "rb") as handle:
            assert handle.getframerate() == 24000
        assert result.duration_ms == 10

    @pytest.mark.asyncio
    async def test_blank_chunks_make_no_request(self, fake_client):
        result = await SparkTTSAdapter().synthesize(
            chunks=[SpeakableChunk(text="   ")],
            voice_id="spark_female_warm",
        )

        assert result.audio_bytes == b""
        assert result.content_type == "audio/wav"
        assert fake_client.requests == []

    @pytest.mark.asyncio
    async def test_api_key_is_forwarded_when_set(self, fake_client):
        await SparkTTSAdapter().synthesize(
            chunks=[SpeakableChunk(text="One.")],
            voice_id="spark_female_warm",
            api_key="secret",
        )

        assert fake_client.requests[0]["headers"]["Authorization"] == "Bearer secret"


class TestListVoices:
    @pytest.mark.asyncio
    async def test_returns_presets_from_the_sidecar(self, fake_client):
        fake_client.get_response = b'[{"id": "spark_female_warm", "name": "Warm Female"}]'

        voices = await SparkTTSAdapter().list_voices(language="en")

        assert voices == [{"id": "spark_female_warm", "name": "Warm Female"}]
        assert fake_client.requests[0]["params"] == {"language": "en"}

    @pytest.mark.asyncio
    async def test_unreachable_sidecar_yields_no_voices(self, fake_client):
        fake_client.get_error = RuntimeError("connection refused")

        assert await SparkTTSAdapter().list_voices() == []

    @pytest.mark.asyncio
    async def test_entries_without_an_id_are_dropped(self, fake_client):
        fake_client.get_response = b'[{"name": "nameless"}, {"id": "ok", "name": "Ok"}]'

        assert await SparkTTSAdapter().list_voices() == [{"id": "ok", "name": "Ok"}]
