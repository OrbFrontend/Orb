"""Test TTS voice enrollment through the workflow upload route, and forgetting a voice."""

from __future__ import annotations

import io
import wave

import pytest

from backend.workflows import spark_tts_host

VALID = list(range(32))
EXCERPT = [7, 8, 9] * 150
SAID = "To fight the abyss, one must know it."


def _wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x10\x00" * int(rate * seconds))
    return buffer.getvalue()


async def _make_char(client) -> str:
    return await client.create("/api/characters", json={"name": "Cloned"})


def _upload_url(card_id: str, mode: str | None = None) -> str:
    query = f"?mode={mode}" if mode else ""
    return f"/api/characters/{card_id}/workflows/tts/upload{query}"


async def _clear(client, card_id: str):
    """Forget the voice the way the panel does: the trigger, in a chat with the character."""
    conv = (await client.post("/api/conversations", json={"character_card_id": card_id})).json()
    return await client.post(f"/api/conversations/{conv['id']}/workflows/tts/trigger", json={"action": "clear_voice"})


async def _profile(client, card_id: str) -> dict:
    """The stored TTS profile, read the way the workflow reads it."""
    from backend.database import get_workflow_character_state
    from backend.workflows.tts.synth import WORKFLOW_ID, normalize_profile

    return normalize_profile(await get_workflow_character_state(card_id, WORKFLOW_ID))


@pytest.fixture
def enrolled(monkeypatch):
    """Stub successful enrollment and unavailable advanced models."""

    async def fake_enroll(data: bytes, *, filename: str = "", with_reference: bool = False):
        assert data, "the route must pass the uploaded bytes through"
        if not with_reference:
            return spark_tts_host.Enrollment(list(VALID))
        return spark_tts_host.Enrollment(list(VALID), list(EXCERPT), SAID)

    monkeypatch.setattr(spark_tts_host, "enrollment_ready", lambda settings: (True, ""))
    monkeypatch.setattr(spark_tts_host, "reference_ready", lambda settings: (False, "Advanced cloning needs a download."))
    monkeypatch.setattr(spark_tts_host, "enroll_upload", fake_enroll)


@pytest.fixture
def advanced_ready(monkeypatch, enrolled):
    monkeypatch.setattr(spark_tts_host, "reference_ready", lambda settings: (True, ""))


async def _upload(client, card_id: str, mode: str | None = None):
    return await client.post(_upload_url(card_id, mode), files={"file": ("memo.wav", _wav(), "audio/wav")})


async def test_upload_stores_the_voice_and_selects_the_backend(client, enrolled):
    card_id = await _make_char(client)

    response = await client.post_json(_upload_url(card_id), files={"file": ("memo.wav", _wav(), "audio/wav")})
    body = response
    assert body["speaker_tokens"] == VALID
    assert body["source_name"] == "memo.wav"

    stored = await _profile(client, card_id)
    assert stored["speaker_tokens"] == VALID
    assert stored["backend"] == "spark"
    assert stored["voice_id"] == "cloned"
    assert stored["speaker_ref_name"] == "memo.wav"
    assert stored["enabled"] is True


async def test_clearing_removes_the_tokens(client, enrolled):
    card_id = await _make_char(client)
    await client.post(_upload_url(card_id), files={"file": ("memo.wav", _wav(), "audio/wav")})

    cleared = await _clear(client, card_id)
    assert cleared.status_code == 200
    assert cleared.json()["profile"]["speaker_tokens"] == []

    assert (await _profile(client, card_id))["speaker_tokens"] == []
    assert (await _profile(client, card_id))["backend"] == "spark"
    assert (await _profile(client, card_id))["enabled"] is False


async def test_enrollment_leaves_the_rest_of_the_profile_intact(client, enrolled):
    """Enrollment preserves unrelated profile fields."""
    from backend.database import set_workflow_character_state
    from backend.workflows.tts.synth import WORKFLOW_ID, normalize_profile

    card_id = await _make_char(client)
    await set_workflow_character_state(
        card_id, WORKFLOW_ID, normalize_profile({"backend": "elevenlabs", "api_key": "secret", "rate": 1.4, "enabled": True})
    )
    await client.post(_upload_url(card_id), files={"file": ("memo.wav", _wav(), "audio/wav")})

    stored = await _profile(client, card_id)
    assert stored["api_key"] == "secret"
    assert stored["rate"] == 1.4
    assert stored["enabled"] is True
    assert stored["backend"] == "spark"


async def test_an_upload_also_prepares_the_advanced_reference_when_it_can(client, advanced_ready):
    card_id = await _make_char(client)

    response = await _upload(client, card_id, "advanced")
    assert response.status_code == 200
    assert response.json()["reference_note"] == ""
    stored = await _profile(client, card_id)
    assert stored["clone_mode"] == "advanced"
    assert stored["reference_tokens"] == EXCERPT
    assert stored["reference_text"] == SAID


async def test_the_reference_is_prepared_from_the_basic_tab_too(client, advanced_ready):
    """Basic enrollment also prepares the advanced reference."""
    card_id = await _make_char(client)
    await _upload(client, card_id, "basic")
    stored = await _profile(client, card_id)
    assert stored["clone_mode"] == "basic"
    assert stored["reference_tokens"] == EXCERPT


async def test_without_the_advanced_models_the_upload_enrolls_and_says_why(client, enrolled):
    card_id = await _make_char(client)
    response = await _upload(client, card_id, "advanced")
    assert response.status_code == 200
    assert response.json()["reference_note"] == "Advanced cloning needs a download."
    stored = await _profile(client, card_id)
    assert stored["speaker_tokens"] == VALID
    assert stored["clone_mode"] == "advanced"
    assert stored["reference_tokens"] == []


async def test_a_new_clip_replaces_the_old_reference(client, monkeypatch, advanced_ready):
    """A new clip clears the previous reference."""
    card_id = await _make_char(client)
    await _upload(client, card_id, "advanced")
    monkeypatch.setattr(spark_tts_host, "reference_ready", lambda settings: (False, "switched off"))
    await _upload(client, card_id)

    stored = await _profile(client, card_id)
    assert stored["reference_tokens"] == []
    assert stored["reference_text"] == ""
    assert stored["clone_mode"] == "advanced"


async def test_an_unknown_mode_keeps_the_profiles(client, advanced_ready):
    card_id = await _make_char(client)
    await _upload(client, card_id, "advanced")
    await _upload(client, card_id, "expert")
    assert (await _profile(client, card_id))["clone_mode"] == "advanced"


async def test_clearing_removes_the_reference(client, advanced_ready):
    card_id = await _make_char(client)
    await _upload(client, card_id, "advanced")
    await _clear(client, card_id)
    stored = await _profile(client, card_id)
    assert stored["reference_tokens"] == []
    assert stored["reference_text"] == ""


async def test_an_unknown_card_is_404(client, enrolled):
    await client.post_checked(
        _upload_url("does-not-exist"), files={"file": ("memo.wav", _wav(), "audio/wav")}, expected_status=404
    )


async def test_an_oversized_upload_is_rejected_before_it_is_decoded(client, enrolled):
    card_id = await _make_char(client)
    response = await client.post_json(
        _upload_url(card_id), files={"file": ("huge.wav", b"\x00" * (26 * 1024 * 1024), "audio/wav")}, expected_status=400
    )
    assert "25 MB" in response["detail"]


async def test_an_unreadable_file_is_a_400_not_a_500(client, monkeypatch):
    card_id = await _make_char(client)
    monkeypatch.setattr(spark_tts_host, "enrollment_ready", lambda settings: (True, ""))
    await client.post_checked(
        _upload_url(card_id), files={"file": ("notes.txt", b"this is not audio at all", "text/plain")}, expected_status=400
    )


async def test_a_missing_model_is_a_503_naming_what_is_missing(client):
    """No stub: this is what CI, and any fresh install, actually gets."""
    card_id = await _make_char(client)
    ready, _reason = spark_tts_host.enrollment_ready({})
    if ready:
        pytest.skip("Spark-TTS codec is installed on this machine")
    response = await client.post_json(
        _upload_url(card_id), files={"file": ("memo.wav", _wav(), "audio/wav")}, expected_status=503
    )
    assert response["detail"]


async def test_a_disabled_workflow_refuses_uploads(client, enrolled):
    card_id = await _make_char(client)
    await client.post("/api/workflows/tts/enabled", json={"enabled": False})
    response = await _upload(client, card_id)
    assert response.status_code == 404
    assert (await _profile(client, card_id))["speaker_tokens"] == []
