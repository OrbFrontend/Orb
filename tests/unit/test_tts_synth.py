"""Unit tests for the TTS workflow's synthesis logic.

Covers profile normalization, the reproduction record (seed + generation
metadata), and the determinism the reroll/rehydrate paths depend on -- all
with a stub adapter, no network or DB.
"""

from __future__ import annotations

import pytest

from backend.workflows.tts import hooks, synth
from backend.workflows.tts.engine.base import SynthesisResult, TTSAdapter


class _FakeAdapter(TTSAdapter):
    """Returns audio bytes derived from the synthesis inputs, so identical
    inputs yield identical bytes (the property reroll/rehydrate rely on)."""

    @property
    def backend_name(self) -> str:
        return "Fake"

    async def synthesize(self, chunks, voice_id, language="en-US", rate=1.0, pitch=1.0, **kwargs):
        payload = repr((voice_id, language, rate, pitch, [c.text for c in chunks])).encode("utf-8")
        return SynthesisResult(audio_bytes=b"FAKE:" + payload, content_type="audio/mpeg")

    async def list_voices(self, language="", **kwargs):
        return []


class _BoundaryAdapter(TTSAdapter):
    """Reports native word boundaries -- one per whitespace token of the chunk
    text, 100ms apart -- so the reconcile path can be told apart from the
    char-proportional estimator."""

    @property
    def backend_name(self) -> str:
        return "Boundary"

    async def synthesize(self, chunks, voice_id, language="en-US", rate=1.0, pitch=1.0, **kwargs):
        text = chunks[0].text if chunks else ""
        boundaries = []
        t = 0
        for tok in text.split():
            boundaries.append({"text": tok, "start_ms": t, "end_ms": t + 100})
            t += 100
        return SynthesisResult(audio_bytes=b"FAKE:" + text.encode("utf-8"), word_boundaries=boundaries or None)

    async def list_voices(self, language="", **kwargs):
        return []


def _patch_adapter(monkeypatch):
    monkeypatch.setattr("backend.workflows.tts.synth.get_adapter", lambda backend: _FakeAdapter())


def test_normalize_profile_fills_defaults():
    p = synth.normalize_profile(None)
    assert p["backend"] == "edge"
    assert p["enabled"] is False
    assert "endpoint_id" not in p


def test_normalize_profile_coerces_types():
    p = synth.normalize_profile({"rate": "1.5", "pitch": "0.8", "enabled": 1, "voice_id": "v9"})
    assert p["rate"] == 1.5
    assert p["pitch"] == 0.8
    assert p["enabled"] is True
    assert p["voice_id"] == "v9"


def test_compute_seed_deterministic_and_sensitive():
    p = synth.normalize_profile({"backend": "edge", "voice_id": "v1"})
    seed = synth.compute_seed("hello", p)
    assert seed and isinstance(seed, str)
    assert synth.compute_seed("hello", p) == seed
    assert synth.compute_seed("world", p) != seed
    assert synth.compute_seed("hello", synth.normalize_profile({"voice_id": "v2"})) != seed


def test_generation_metadata_is_self_contained():
    p = synth.normalize_profile({"backend": "openai", "voice_id": "nova", "api_key": "sk-secret", "model": "tts-1"})
    md = synth.build_generation_metadata("speak this", p)
    assert md["text"] == "speak this"
    assert md["voice_id"] == "nova"
    assert md["model"] == "tts-1"
    assert md["api_key"] == "sk-secret"


async def test_reroll_gen_ignores_seed_and_reproduces_bytes(monkeypatch):
    _patch_adapter(monkeypatch)
    md = synth.build_generation_metadata('"Hello there."', synth.normalize_profile({"voice_id": "v1"}))
    first = await hooks.reroll_gen(None, md, "framework-seed-A")
    second = await hooks.reroll_gen(None, md, "framework-seed-B")
    audio, cm = first
    assert isinstance(audio, bytes) and audio
    assert isinstance(cm["blocks"], list) and cm["blocks"]
    assert all("words" in b and isinstance(b["words"], list) for b in cm["blocks"])
    assert first == second


async def test_synthesize_blocks_from_metadata_requires_text(monkeypatch):
    _patch_adapter(monkeypatch)
    with pytest.raises(ValueError):
        await synth.synthesize_blocks_from_metadata({"backend": "edge", "voice_id": "v1"})


async def test_synthesize_blocks_segments_each_dialogue(monkeypatch):
    _patch_adapter(monkeypatch)
    profile = synth.normalize_profile({"voice_id": "v1"})
    audio, mime, blocks = await synth.synthesize_blocks('"First line." then "second line."', profile)
    assert len(blocks) == 2
    # Ranges are contiguous and cover the whole concatenation.
    assert blocks[0]["byte_start"] == 0
    assert blocks[0]["byte_end"] == blocks[1]["byte_start"]
    assert blocks[-1]["byte_end"] == len(audio)
    # The inter-dialogue gap is carried as the first block's trailing pause; the
    # last block has none.
    assert blocks[0]["pause_after_ms"] > 0
    assert blocks[-1]["pause_after_ms"] == 0
    assert mime


async def test_synthesize_blocks_raises_without_dialogue(monkeypatch):
    _patch_adapter(monkeypatch)
    profile = synth.normalize_profile({"voice_id": "v1"})
    with pytest.raises(ValueError):
        await synth.synthesize_blocks("Narration only, no quotes here.", profile)


def test_alignable_tokens_keeps_alnum_drops_punctuation_and_nonascii():
    assert synth._alignable_tokens("hello world") == ["hello", "world"]
    assert synth._alignable_tokens("-- ... !!!") == []
    # A CJK-only token (here U+4F60 U+597D) carries no ASCII letter or digit and
    # is dropped; the ASCII word survives.
    assert synth._alignable_tokens("你好 hi") == ["hi"]
    # Trailing punctuation rides along on an otherwise-alphanumeric token.
    assert synth._alignable_tokens("Hi, friend!") == ["Hi,", "friend!"]


def test_estimate_word_spans_count_monotonic_and_empty():
    spans = synth.estimate_word_spans("Hello there friend")
    assert len(spans) == 3
    starts = [s["start_ms"] for s in spans]
    assert starts == sorted(starts)
    assert all(s["end_ms"] >= s["start_ms"] for s in spans)
    assert all(spans[i + 1]["start_ms"] >= spans[i]["start_ms"] for i in range(len(spans) - 1))
    assert synth.estimate_word_spans("-- ...") == []
    assert synth.estimate_word_spans("") == []


def test_reconcile_boundaries_one_to_one():
    spans = synth.reconcile_boundaries(
        "Hello there",
        [{"text": "Hello", "start_ms": 0, "end_ms": 100}, {"text": "there", "start_ms": 100, "end_ms": 250}],
    )
    assert spans == [{"start_ms": 0.0, "end_ms": 100.0}, {"start_ms": 100.0, "end_ms": 250.0}]


def test_reconcile_boundaries_aligns_across_punctuation():
    # The backend word text omits the comma/bang our token keeps; character
    # overlap still attributes each boundary to the right token.
    spans = synth.reconcile_boundaries(
        "Hi, friend!",
        [{"text": "Hi", "start_ms": 0, "end_ms": 80}, {"text": "friend", "start_ms": 80, "end_ms": 200}],
    )
    assert spans is not None
    assert len(spans) == 2
    assert spans[0]["start_ms"] == 0.0
    assert spans[1]["end_ms"] == 200.0


def test_reconcile_boundaries_merges_multiword_boundary():
    # One boundary spanning two of our tokens: both take its union.
    spans = synth.reconcile_boundaries("New York", [{"text": "New York", "start_ms": 0, "end_ms": 300}])
    assert spans == [{"start_ms": 0.0, "end_ms": 300.0}, {"start_ms": 0.0, "end_ms": 300.0}]


def test_reconcile_boundaries_unions_split_within_one_token():
    # Two boundaries inside a single whitespace token: the token takes their
    # union (min start, max end).
    spans = synth.reconcile_boundaries(
        "well-being",
        [{"text": "well", "start_ms": 0, "end_ms": 90}, {"text": "being", "start_ms": 90, "end_ms": 200}],
    )
    assert spans == [{"start_ms": 0.0, "end_ms": 200.0}]


def test_reconcile_boundaries_returns_none_on_divergence():
    # A token no boundary can cover -> incomplete mapping -> None (caller estimates).
    assert synth.reconcile_boundaries("Hello", [{"text": "Goodbye", "start_ms": 0, "end_ms": 100}]) is None
    assert synth.reconcile_boundaries("Hello", []) is None
    assert synth.reconcile_boundaries("", [{"text": "x", "start_ms": 0, "end_ms": 1}]) is None


async def test_synthesize_blocks_estimates_words_without_native_timing(monkeypatch):
    _patch_adapter(monkeypatch)
    profile = synth.normalize_profile({"voice_id": "v1"})
    _, _, blocks = await synth.synthesize_blocks('"Hello there."', profile)
    words = blocks[0]["words"]
    # One span per alignable token ("Hello", "there."), char-proportional widths.
    assert len(words) == 2
    assert words[0]["start_ms"] == 0.0
    assert words[1]["start_ms"] == float(len("Hello"))


async def test_synthesize_blocks_uses_native_word_boundaries(monkeypatch):
    monkeypatch.setattr("backend.workflows.tts.synth.get_adapter", lambda backend: _BoundaryAdapter())
    profile = synth.normalize_profile({"voice_id": "v1"})
    _, _, blocks = await synth.synthesize_blocks('"Hello there."', profile)
    words = blocks[0]["words"]
    assert len(words) == 2
    # 100ms-apart native boundaries, not the estimator's char-proportional widths.
    assert words[0]["end_ms"] == 100.0
    assert words[1]["start_ms"] == 100.0
