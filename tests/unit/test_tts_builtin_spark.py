"""Tests for the built-in Spark cloner profile and adapter."""

from __future__ import annotations

import pytest

from backend.workflows.tts.engine.base import SpeakableChunk
from backend.workflows.tts.engine.builtin_spark_adapter import BuiltinSparkAdapter
from backend.workflows.tts.engine.router import get_adapter, list_backends
from backend.workflows.tts.engine.wav import strip_header
from backend.workflows.tts.synth import (
    _METADATA_KEYS,
    PROFILE_DEFAULTS,
    _backend_kwargs,
    audio_mime_ext,
    build_generation_metadata,
    compute_seed,
    normalize_profile,
    speaks_advanced,
)

VALID = list(range(32))
EXCERPT = [8191, 0, 4096] * 100
ADVANCED = {
    "backend": "spark",
    "speaker_tokens": VALID,
    "clone_mode": "advanced",
    "reference_tokens": EXCERPT,
    "reference_text": "What better laboratory than the blood-soaked battlefield?",
}


def test_the_sidecar_keeps_a_name_of_its_own():
    """Legacy sidecar profiles continue to resolve as ``spark_remote``."""
    assert isinstance(get_adapter("spark"), BuiltinSparkAdapter)
    assert get_adapter("spark_remote").backend_name == "Spark-TTS (sidecar)"
    assert {b["id"] for b in list_backends()} >= {"spark", "spark_remote"}


def test_both_spark_backends_are_declared_wav():
    """Both Spark backends return WAV audio."""
    assert audio_mime_ext("spark") == ("audio/wav", "wav")
    assert audio_mime_ext("spark_remote") == ("audio/wav", "wav")


def test_profile_defaults_carry_an_empty_voice():
    assert PROFILE_DEFAULTS["speaker_tokens"] == []
    assert PROFILE_DEFAULTS["speaker_ref_name"] == ""


@pytest.mark.parametrize("bad", [None, "x", [1], list(range(33)), [4096] * 32, [1.5] * 32, [True] * 32])
def test_a_malformed_voice_normalizes_to_no_voice(bad):
    """Malformed speaker tokens normalize to an empty voice."""
    assert normalize_profile({"backend": "spark", "speaker_tokens": bad})["speaker_tokens"] == []


def test_a_valid_voice_survives_normalization():
    profile = normalize_profile({"backend": "spark", "speaker_tokens": VALID, "speaker_ref_name": "memo.m4a"})
    assert profile["speaker_tokens"] == VALID
    assert profile["speaker_ref_name"] == "memo.m4a"


def test_the_voice_is_part_of_the_reproduction_record():
    """Speaker tokens are included in generation metadata."""
    assert "speaker_tokens" in _METADATA_KEYS
    profile = normalize_profile({"backend": "spark", "speaker_tokens": VALID})
    assert build_generation_metadata("hello", profile)["speaker_tokens"] == VALID
    # Metadata round-trips through the profile normalizer.
    assert normalize_profile(build_generation_metadata("hello", profile))["speaker_tokens"] == VALID


def test_an_advanced_voice_survives_normalization():
    profile = normalize_profile(ADVANCED)
    assert profile["clone_mode"] == "advanced"
    assert profile["reference_tokens"] == EXCERPT
    assert speaks_advanced(profile)


def test_an_advanced_reference_is_ignored_by_other_backends():
    profile = normalize_profile({**ADVANCED, "backend": "edge"})
    assert not speaks_advanced(profile)
    record = build_generation_metadata("hello", profile)
    assert record["reference_tokens"] == []
    assert record["reference_text"] == ""


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("reference_tokens", [8192]),
        ("reference_tokens", [True, 1]),
        ("reference_tokens", list(range(601))),
        ("reference_tokens", "1 2 3"),
    ],
)
def test_a_malformed_excerpt_normalizes_to_none(field, bad):
    """A bad excerpt must not reach the prompt, where it would be read as speech."""
    profile = normalize_profile({**ADVANCED, field: bad})
    assert profile["reference_tokens"] == []
    assert not speaks_advanced(profile)


def test_an_unknown_clone_mode_is_basic():
    assert normalize_profile({**ADVANCED, "clone_mode": "expert"})["clone_mode"] == "basic"


def test_the_transcript_is_kept_on_one_line_and_within_the_limit():
    profile = normalize_profile({**ADVANCED, "reference_text": "  one\n two   three " + "x" * 1000})
    assert profile["reference_text"].startswith("one two three x")
    assert len(profile["reference_text"]) == 400


@pytest.mark.parametrize(
    "change",
    [{"clone_mode": "basic"}, {"reference_text": "   "}, {"reference_tokens": []}],
)
def test_advanced_speech_needs_the_tab_and_both_halves_of_the_reference(change):
    profile = normalize_profile({**ADVANCED, **change})
    assert not speaks_advanced(profile)
    kwargs = _backend_kwargs(profile, None)
    assert kwargs["reference_tokens"] == [] and kwargs["reference_text"] == ""


def test_an_advanced_voice_sends_its_reference_to_the_adapter():
    kwargs = _backend_kwargs(normalize_profile(ADVANCED), None)
    assert kwargs["reference_tokens"] == EXCERPT
    assert kwargs["reference_text"] == ADVANCED["reference_text"]


def test_the_reference_is_part_of_the_reproduction_record_only_when_spoken():
    """Include the reference in metadata only for advanced speech."""
    assert {"clone_mode", "reference_tokens", "reference_text"} <= set(_METADATA_KEYS)
    advanced = normalize_profile(ADVANCED)
    record = build_generation_metadata("hello", advanced)
    assert speaks_advanced(normalize_profile(record))

    basic = normalize_profile({**ADVANCED, "clone_mode": "basic"})
    record = build_generation_metadata("hello", basic)
    assert record["reference_tokens"] == [] and record["reference_text"] == ""
    assert compute_seed("hello", basic) == compute_seed("hello", {**basic, "reference_tokens": [], "reference_text": ""})
    assert compute_seed("hello", advanced) != compute_seed("hello", basic)


async def test_the_adapter_passes_the_reference_to_every_chunk(monkeypatch):
    import backend.workflows.tts.engine.builtin_spark_adapter as module

    seen = []

    async def fake_speak(text, speaker_tokens, settings, *, reference_tokens=(), reference_text=""):
        seen.append((text, list(reference_tokens), reference_text))
        return b"\x01\x00" * 160, 16000

    monkeypatch.setattr(module, "spark_voice_speak", fake_speak)
    await module.BuiltinSparkAdapter().synthesize(
        chunks=[SpeakableChunk(text="one"), SpeakableChunk(text="two")],
        voice_id="cloned",
        settings={},
        **{k: v for k, v in _backend_kwargs(normalize_profile(ADVANCED), {}).items() if k != "settings"},
    )
    assert seen == [(t, EXCERPT, ADVANCED["reference_text"]) for t in ("one", "two")]


async def test_synthesis_without_an_enrolled_voice_says_so():
    adapter = BuiltinSparkAdapter()
    with pytest.raises(ValueError, match="no cloned voice"):
        await adapter.synthesize(chunks=[SpeakableChunk(text="hi")], voice_id="cloned", speaker_tokens=[])


async def test_empty_text_is_not_an_error():
    """The extractor can legitimately produce nothing speakable."""
    result = await BuiltinSparkAdapter().synthesize(chunks=[SpeakableChunk(text="   ")], voice_id="cloned")
    assert result.audio_bytes == b""


async def test_synthesis_surfaces_the_readiness_reason_verbatim():
    """Synthesis reports the readiness failure reason."""
    from backend.workflows import spark_tts_host

    settings = {"local_ml_enabled": {}}
    ready, reason = spark_tts_host.synthesis_ready(settings)
    if ready:
        pytest.skip("Spark-TTS is fully installed on this machine")
    with pytest.raises(ValueError) as excinfo:
        await BuiltinSparkAdapter().synthesize(
            chunks=[SpeakableChunk(text="hi")],
            voice_id="cloned",
            speaker_tokens=VALID,
            settings=settings,
        )
    assert str(excinfo.value) == reason


async def test_a_disabled_local_ml_toggle_is_reported_as_such():
    adapter = BuiltinSparkAdapter()
    with pytest.raises(ValueError, match="switched off"):
        await adapter.synthesize(
            chunks=[SpeakableChunk(text="hi")],
            voice_id="cloned",
            speaker_tokens=VALID,
            settings={"local_ml_enabled": {"spark_tts_llm": False}},
        )


async def test_list_voices_is_empty_without_a_picker():
    adapter = BuiltinSparkAdapter()
    assert await adapter.list_voices(speaker_tokens=[]) == []
    assert await adapter.list_voices(speaker_tokens=VALID, speaker_ref_name="memo.wav") == []


async def test_chunk_pauses_become_real_silence_between_clips(monkeypatch):
    """Chunk pauses become silence between clips."""
    import backend.workflows.tts.engine.builtin_spark_adapter as module

    async def fake_speak(text, speaker_tokens, settings, **_reference):
        return b"\x01\x00" * 1600, 16000  # 0.1 s of non-silence per chunk

    monkeypatch.setattr(module, "spark_voice_speak", fake_speak)
    result = await module.BuiltinSparkAdapter().synthesize(
        chunks=[
            SpeakableChunk(text="one", pause_before_ms=500, pause_after_ms=200),
            SpeakableChunk(text="two", pause_before_ms=300),
        ],
        voice_id="cloned",
        speaker_tokens=VALID,
        settings={},
    )
    pcm, rate = strip_header(result.audio_bytes)
    assert rate == 16000
    # 0.1 + 0.2 + 0.3 + 0.1 seconds; the leading 500 ms is not in there.
    assert len(pcm) == int(16000 * 0.7) * 2
    assert result.duration_ms == 700


@pytest.mark.parametrize("gpu", [True, False])
def test_the_spark_child_never_runs_a_prompt_batch_past_eight(tmp_path, monkeypatch, gpu):
    """The Spark child keeps its prompt batch at or below eight."""
    from backend.inference.local_models.llama_server import client as C
    from backend.inference.local_models.spark_tts import config

    gguf = tmp_path / "spark.gguf"
    gguf.write_text("gguf")
    monkeypatch.setattr(config.assets, "resolve_path", lambda _feature: str(gguf))
    monkeypatch.setattr(C.binary_module, "supports_flag", lambda _binary, _flag: False)
    argv = C._argv(config.launch_profile(gpu=gpu), C.Path("llama-server"), 1)

    assert 0 < int(argv[argv.index("--ubatch-size") + 1]) <= 8
