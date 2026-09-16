"""The built-in Spark cloner's profile contract and adapter behaviour.

No model here: the point is everything that happens BEFORE one is loaded, which
is where a cloned voice is silently lost or silently wrong.
"""

from __future__ import annotations

import pytest

from backend.workflows.tts.engine.base import SpeakableChunk
from backend.workflows.tts.engine.builtin_spark_adapter import BuiltinSparkAdapter
from backend.workflows.tts.engine.router import get_adapter, list_backends
from backend.workflows.tts.engine.wav import strip_header
from backend.workflows.tts.synth import (
    _METADATA_KEYS,
    PROFILE_DEFAULTS,
    audio_mime_ext,
    build_generation_metadata,
    normalize_profile,
)

VALID = list(range(32))


def test_the_sidecar_keeps_a_name_of_its_own():
    """Existing profiles point at a server the user installed and runs. The
    built-in takes `spark`; migration 0062 moves those saves to `spark_remote`,
    which must therefore still resolve."""
    assert isinstance(get_adapter("spark"), BuiltinSparkAdapter)
    assert get_adapter("spark_remote").backend_name == "Spark-TTS (sidecar)"
    assert {b["id"] for b in list_backends()} >= {"spark", "spark_remote"}


def test_both_spark_backends_are_declared_wav():
    """The sidecar emitted WAV and so does the built-in; a backend missing from
    that set has its clips labelled audio/mpeg and plays as noise or not at all."""
    assert audio_mime_ext("spark") == ("audio/wav", "wav")
    assert audio_mime_ext("spark_remote") == ("audio/wav", "wav")


def test_profile_defaults_carry_an_empty_voice():
    assert PROFILE_DEFAULTS["speaker_tokens"] == []
    assert PROFILE_DEFAULTS["speaker_ref_name"] == ""


@pytest.mark.parametrize("bad", [None, "x", [1], list(range(33)), [4096] * 32, [1.5] * 32, [True] * 32])
def test_a_malformed_voice_normalizes_to_no_voice(bad):
    """It must not reach the codec, which answers a wrong-length array with an
    exception from inside an einsum rather than a message."""
    assert normalize_profile({"backend": "spark", "speaker_tokens": bad})["speaker_tokens"] == []


def test_a_valid_voice_survives_normalization():
    profile = normalize_profile({"backend": "spark", "speaker_tokens": VALID, "speaker_ref_name": "memo.m4a"})
    assert profile["speaker_tokens"] == VALID
    assert profile["speaker_ref_name"] == "memo.m4a"


def test_the_voice_is_part_of_the_reproduction_record():
    """`_METADATA_KEYS` is what a reroll re-synthesizes from, in a context with
    no character state. Without the tokens, rerolling a cloned line comes back
    in a different voice."""
    assert "speaker_tokens" in _METADATA_KEYS
    profile = normalize_profile({"backend": "spark", "speaker_tokens": VALID})
    assert build_generation_metadata("hello", profile)["speaker_tokens"] == VALID
    # And the record round-trips back through the normaliser a reroll uses.
    assert normalize_profile(build_generation_metadata("hello", profile))["speaker_tokens"] == VALID


async def test_synthesis_without_an_enrolled_voice_says_so():
    adapter = BuiltinSparkAdapter()
    with pytest.raises(ValueError, match="no cloned voice"):
        await adapter.synthesize(chunks=[SpeakableChunk(text="hi")], voice_id="cloned", speaker_tokens=[])


async def test_empty_text_is_not_an_error():
    """The extractor can legitimately produce nothing speakable."""
    result = await BuiltinSparkAdapter().synthesize(chunks=[SpeakableChunk(text="   ")], voice_id="cloned")
    assert result.audio_bytes == b""


async def test_synthesis_surfaces_the_readiness_reason_verbatim():
    """Which piece is missing depends on the machine — no onnxruntime, no GGUF,
    no llama-server — so the contract is that whatever readiness says is what
    the user is told, not that the adapter invents a message of its own."""
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
    """The stitching contract the other local backends share: a pause hint is
    rendered as PCM, and the leading pause on the first chunk is dropped
    because it would only delay playback."""
    import backend.workflows.tts.engine.builtin_spark_adapter as module

    async def fake_speak(text, speaker_tokens, settings):
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
    """Above 8 rows ggml-vulkan switches kernels, and that one garbles this
    model's prompt: a second of babble instead of the line, with no error
    anywhere. See `UBATCH_SIZE` — raising it is a regression, not a speedup."""
    from backend.inference.local_models.llama_server import client as C
    from backend.inference.local_models.spark_tts import config

    gguf = tmp_path / "spark.gguf"
    gguf.write_text("gguf")
    monkeypatch.setattr(config.assets, "resolve_path", lambda _feature: str(gguf))
    monkeypatch.setattr(C.binary_module, "supports_flag", lambda _binary, _flag: False)
    argv = C._argv(config.launch_profile(gpu=gpu), C.Path("llama-server"), 1)

    assert 0 < int(argv[argv.index("--ubatch-size") + 1]) <= 8
