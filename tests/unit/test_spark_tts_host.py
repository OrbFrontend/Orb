"""The Spark-TTS host's advanced-cloning composition, with the models stubbed."""

from __future__ import annotations

import pytest

from backend.inference.local_models import whisper
from backend.inference.local_models.spark_tts import reference, service
from backend.workflows import spark_tts_host as host

VALID = list(range(32))
EXCERPT = [1, 2, 3]


@pytest.mark.parametrize(
    ("feature", "label"),
    [("spark_tts_reference", "reference reader"), ("speech_recognizer", "speech recognizer")],
)
def test_reference_readiness_names_the_switch_that_is_off(monkeypatch, feature, label):
    monkeypatch.setattr(host, "enrollment_ready", lambda settings: (True, ""))
    ok, reason = host.reference_ready({"local_ml_enabled": {feature: False}})
    assert not ok
    assert label in reason and "switched off" in reason


def test_reference_readiness_needs_the_codec_first(monkeypatch):
    monkeypatch.setattr(host, "enrollment_ready", lambda settings: (False, "codec missing"))
    assert host.reference_ready({}) == (False, "codec missing")


@pytest.fixture
def stubbed(monkeypatch):
    """Excerpt, tokenizer and recognizer stand-ins; records what was released."""
    released: list[str] = []
    heard = {"value": whisper.Transcript(text="  One must   know it. ", complete=True)}
    monkeypatch.setattr(host.audio_in, "volume_normalize", lambda wav: wav)
    monkeypatch.setattr(reference, "select_excerpt", lambda wav: wav)
    monkeypatch.setattr(reference, "semantic_tokens", lambda excerpt: list(EXCERPT))
    monkeypatch.setattr(reference, "release", lambda: released.append("reference"))
    monkeypatch.setattr(whisper, "files", lambda: "files")
    monkeypatch.setattr(whisper, "transcribe", lambda excerpt, files: heard["value"])
    monkeypatch.setattr(whisper, "release", lambda files: released.append("whisper"))
    return released, heard


def test_a_prepared_reference_is_tokens_and_a_tidy_transcript(stubbed):
    released, _heard = stubbed
    assert host._prepare_reference([0.0]) == (EXCERPT, "One must know it.", "")
    assert released == ["reference", "whisper"]  # large, and needed once per voice


def test_an_unfinished_transcript_keeps_the_excerpt_and_asks_for_typing(stubbed):
    _released, heard = stubbed
    heard["value"] = whisper.Transcript(text="you you you", complete=False)
    tokens, text, note = host._prepare_reference([0.0])
    assert tokens == EXCERPT and text == ""
    assert "type what it says" in note


def test_a_clip_without_an_excerpt_says_why(stubbed, monkeypatch):
    released, _heard = stubbed

    def refuse(_wav):
        raise reference.NoUsableExcerpt("The clip needs at least 2 seconds of clear speech for advanced cloning.")

    monkeypatch.setattr(reference, "select_excerpt", refuse)
    assert host._prepare_reference([0.0]) == ([], "", "The clip needs at least 2 seconds of clear speech for advanced cloning.")
    assert released == ["reference", "whisper"]


def test_a_failing_model_does_not_fail_the_enrollment(stubbed, monkeypatch, caplog):
    def broken(_excerpt):
        raise RuntimeError("bad graph")

    monkeypatch.setattr(reference, "semantic_tokens", broken)
    tokens, text, note = host._prepare_reference([0.0])
    assert (tokens, text) == ([], "")
    assert "could not be prepared" in note
    assert "advanced reference preparation failed" in caplog.text


async def test_enroll_upload_prepares_the_reference_only_when_asked(stubbed, monkeypatch):
    monkeypatch.setattr(host.audio_in, "decode", lambda data, filename="": [0.0])
    monkeypatch.setattr(host.enroll, "reference_clip", lambda wav: wav)
    monkeypatch.setattr(host.enroll, "enroll_signal", lambda signal: list(VALID))

    basic = await host.enroll_upload(b"clip")
    assert basic == host.Enrollment(VALID)
    advanced = await host.enroll_upload(b"clip", with_reference=True)
    assert advanced == host.Enrollment(VALID, EXCERPT, "One must know it.", "")


@pytest.mark.parametrize(
    ("tokens", "text", "sent"),
    [(EXCERPT, "One must know it.", True), (EXCERPT, "  ", False), ([], "One must know it.", False)],
)
async def test_synthesis_sends_a_reference_only_with_both_halves(monkeypatch, tokens, text, sent):
    seen = {}

    async def fake_synthesize(line, speaker, *, reference=None, gpu=True):
        seen["reference"] = reference
        return b"", 16000

    monkeypatch.setattr(host, "synthesis_ready", lambda settings: (True, ""))
    monkeypatch.setattr(service, "synthesize", fake_synthesize)
    await host.synthesize("Hi.", VALID, {}, reference_tokens=tokens, reference_text=text)
    assert (seen["reference"] is not None) is sent
    if sent:
        assert seen["reference"] == reference.Reference(text, tokens)
