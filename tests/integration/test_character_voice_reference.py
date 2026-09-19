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
    return (await client.post("/api/characters", json={"name": "Cloned"})).json()["id"]


async def _profile(client, card_id: str) -> dict:
    """The stored TTS profile, read the way the workflow reads it."""
    from backend.database import get_workflow_character_state
    from backend.workflows.tts.synth import WORKFLOW_ID, normalize_profile

    return normalize_profile(await get_workflow_character_state(card_id, WORKFLOW_ID))


@pytest.fixture
def enrolled(monkeypatch):
    """Stand in for the codec half: enrollment succeeds. Advanced cloning is
    not set up unless a test says so."""

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
    query = f"?mode={mode}" if mode else ""
    return await client.post(
        f"/api/characters/{card_id}/voice-reference{query}",
        files={"file": ("memo.wav", _wav(), "audio/wav")},
    )


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
    # ...including the switch that makes it audible. A stored voice that does
    # not speak is the failure mode this route exists to avoid.
    assert stored["enabled"] is True


async def test_clearing_removes_the_tokens(client, enrolled):
    card_id = await _make_char(client)
    await client.post(f"/api/characters/{card_id}/voice-reference", files={"file": ("memo.wav", _wav(), "audio/wav")})

    cleared = await client.delete(f"/api/characters/{card_id}/voice-reference")
    assert cleared.status_code == 200
    assert cleared.json()["profile"]["speaker_tokens"] == []

    assert (await _profile(client, card_id))["speaker_tokens"] == []
    # The backend selection is deliberately left alone: a user clearing a voice
    # to upload a different one should not have to re-pick it. Auto-generation
    # is not: a `spark` profile with no tokens fails once per turn.
    assert (await _profile(client, card_id))["backend"] == "spark"
    assert (await _profile(client, card_id))["enabled"] is False


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
    """So switching to Advanced later needs no second upload."""
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
    assert stored["clone_mode"] == "advanced"  # the tab it was dropped on, speaking Basic until it can
    assert stored["reference_tokens"] == []


async def test_a_new_clip_replaces_the_old_reference(client, monkeypatch, advanced_ready):
    """An excerpt of the last clip would pair its delivery with the new clip's timbre."""
    card_id = await _make_char(client)
    await _upload(client, card_id, "advanced")
    monkeypatch.setattr(spark_tts_host, "reference_ready", lambda settings: (False, "switched off"))
    await _upload(client, card_id)

    stored = await _profile(client, card_id)
    assert stored["reference_tokens"] == []
    assert stored["reference_text"] == ""
    assert stored["clone_mode"] == "advanced"  # no mode given keeps the profile's


async def test_an_unknown_mode_keeps_the_profiles(client, advanced_ready):
    card_id = await _make_char(client)
    await _upload(client, card_id, "advanced")
    await _upload(client, card_id, "expert")
    assert (await _profile(client, card_id))["clone_mode"] == "advanced"


async def test_clearing_removes_the_reference(client, advanced_ready):
    card_id = await _make_char(client)
    await _upload(client, card_id, "advanced")
    await client.delete(f"/api/characters/{card_id}/voice-reference")
    stored = await _profile(client, card_id)
    assert stored["reference_tokens"] == []
    assert stored["reference_text"] == ""


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
