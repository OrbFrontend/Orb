"""The voice-enrollment routes: upload -> stored voice -> clear.

The models are never present in CI, so the 503 path is what runs there and the
happy path runs only where the codec is available. Both matter: "the model is
not downloaded" and "that file is not audio" are different answers.

Enrollment is stubbed in the happy-path tests rather than run for real — what
is under test here is the ROUTE's contract (what it writes, what it selects,
what it returns), and running a real 24 MB ONNX graph would test the enrollment
that ``test_spark_enroll`` already pins.
"""

from __future__ import annotations

import io
import wave

import pytest

from backend.workflows import spark_tts_host

VALID = list(range(32))


def _wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x10\x00" * int(rate * seconds))
    return buffer.getvalue()


async def _make_char(client) -> str:
    return (await client.post("/api/characters", json={"name": "Cloned"})).json()["id"]


async def _profile(client, card_id: str) -> dict:
    """The stored TTS profile, read the way the workflow reads it."""
    from backend.database import get_workflow_character_state
    from backend.workflows.tts.synth import WORKFLOW_ID, normalize_profile

    return normalize_profile(await get_workflow_character_state(card_id, WORKFLOW_ID))


@pytest.fixture
def enrolled(monkeypatch):
    """Stand in for the codec half: enrollment succeeds."""

    async def fake_enroll(data: bytes, *, filename: str = ""):
        assert data, "the route must pass the uploaded bytes through"
        return list(VALID)

    monkeypatch.setattr(spark_tts_host, "enrollment_ready", lambda settings: (True, ""))
    monkeypatch.setattr(spark_tts_host, "enroll_upload", fake_enroll)


async def test_upload_stores_the_voice_and_selects_the_backend(client, enrolled):
    card_id = await _make_char(client)

    response = await client.post(
        f"/api/characters/{card_id}/voice-reference",
        files={"file": ("memo.wav", _wav(), "audio/wav")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["speaker_tokens"] == VALID
    assert body["source_name"] == "memo.wav"

    # Uploading a clip IS the whole workflow: the profile comes back pointed at
    # the built-in backend, so the user does not have to find two more dropdowns.
    stored = await _profile(client, card_id)
    assert stored["speaker_tokens"] == VALID
    assert stored["backend"] == "spark"
    assert stored["voice_id"] == "cloned"
    assert stored["speaker_ref_name"] == "memo.wav"


async def test_clearing_removes_the_tokens(client, enrolled):
    card_id = await _make_char(client)
    await client.post(f"/api/characters/{card_id}/voice-reference", files={"file": ("memo.wav", _wav(), "audio/wav")})

    cleared = await client.delete(f"/api/characters/{card_id}/voice-reference")
    assert cleared.status_code == 200
    assert cleared.json()["profile"]["speaker_tokens"] == []

    assert (await _profile(client, card_id))["speaker_tokens"] == []
    # The backend selection is deliberately left alone: a user clearing a voice
    # to upload a different one should not have to re-pick it.
    assert (await _profile(client, card_id))["backend"] == "spark"


async def test_enrollment_leaves_the_rest_of_the_profile_intact(client, enrolled):
    """Read-modify-write, not overwrite: an API key or rate saved on another
    backend must survive someone trying out a cloned voice."""
    from backend.database import set_workflow_character_state
    from backend.workflows.tts.synth import WORKFLOW_ID, normalize_profile

    card_id = await _make_char(client)
    await set_workflow_character_state(
        card_id,
        WORKFLOW_ID,
        normalize_profile({"backend": "elevenlabs", "api_key": "secret", "rate": 1.4, "enabled": True}),
    )
    await client.post(f"/api/characters/{card_id}/voice-reference", files={"file": ("memo.wav", _wav(), "audio/wav")})

    stored = await _profile(client, card_id)
    assert stored["api_key"] == "secret"
    assert stored["rate"] == 1.4
    assert stored["enabled"] is True
    assert stored["backend"] == "spark"


async def test_an_unknown_card_is_404(client, enrolled):
    response = await client.post(
        "/api/characters/does-not-exist/voice-reference",
        files={"file": ("memo.wav", _wav(), "audio/wav")},
    )
    assert response.status_code == 404


async def test_an_oversized_upload_is_rejected_before_it_is_decoded(client, enrolled):
    card_id = await _make_char(client)
    response = await client.post(
        f"/api/characters/{card_id}/voice-reference",
        files={"file": ("huge.wav", b"\x00" * (26 * 1024 * 1024), "audio/wav")},
    )
    assert response.status_code == 400
    assert "25 MB" in response.json()["detail"]


async def test_an_unreadable_file_is_a_400_not_a_500(client, monkeypatch):
    card_id = await _make_char(client)
    monkeypatch.setattr(spark_tts_host, "enrollment_ready", lambda settings: (True, ""))
    response = await client.post(
        f"/api/characters/{card_id}/voice-reference",
        files={"file": ("notes.txt", b"this is not audio at all", "text/plain")},
    )
    assert response.status_code == 400


async def test_a_missing_model_is_a_503_naming_what_is_missing(client):
    """No stub: this is what CI, and any fresh install, actually gets."""
    card_id = await _make_char(client)
    ready, _reason = spark_tts_host.enrollment_ready({})
    if ready:
        pytest.skip("Spark-TTS codec is installed on this machine")
    response = await client.post(
        f"/api/characters/{card_id}/voice-reference",
        files={"file": ("memo.wav", _wav(), "audio/wav")},
    )
    assert response.status_code == 503
    assert response.json()["detail"]
