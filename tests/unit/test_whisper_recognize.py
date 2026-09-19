"""The greedy Whisper loop, against fake encoder and merged-decoder sessions."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from backend.inference.local_models.whisper import recognize as module  # noqa: E402

SOT, EOS, EN, ZH, TRANSCRIBE, NO_TIMESTAMPS = 10, 9, 11, 12, 13, 15
HEADS, HEAD_DIM, VOCAB_SIZE = 2, 4, 20
# Text tokens 0-8 and end-of-text; 10-19 are special and live only in added tokens.
TEXT = {"Hello": 0, "Ġthere": 1, ".": 2, "âĻª": 3, "Ġ": 4, "<|endoftext|>": EOS}


def _files(tmp_path) -> module.WhisperFiles:
    config = {"num_mel_bins": 80, "vocab_size": VOCAB_SIZE, "decoder_attention_heads": HEADS, "d_model": HEADS * HEAD_DIM}
    generation = {
        "decoder_start_token_id": SOT,
        "eos_token_id": EOS,
        "lang_to_id": {"<|en|>": EN, "<|zh|>": ZH},
        "task_to_id": {"transcribe": TRANSCRIBE, "translate": 14},
        "no_timestamps_token_id": NO_TIMESTAMPS,
        "suppress_tokens": [3],
        "begin_suppress_tokens": [4, EOS],
        "max_length": 448,
    }
    for name, body in (("config.json", config), ("generation.json", generation), ("vocab.json", TEXT)):
        (tmp_path / name).write_text(json.dumps(body), encoding="utf-8")
    return module.WhisperFiles(
        encoder="encoder.onnx",
        decoder="decoder.onnx",
        vocab=str(tmp_path / "vocab.json"),
        config=str(tmp_path / "config.json"),
        generation_config=str(tmp_path / "generation.json"),
    )


class FakeEncoder:
    def run(self, _outputs, feed):
        assert feed["input_features"].shape == (1, 80, 3000)
        return [np.zeros((1, 1500, HEADS * HEAD_DIM), dtype=np.float32)]


class FakeDecoder:
    """Scores the next token with *choose(history)*, checking the cache it is fed."""

    def __init__(self, choose) -> None:
        self.choose = choose
        self.history: list[int] = []
        self.passes = 0

    def get_inputs(self):
        names = ["input_ids", "encoder_hidden_states"]
        names += [f"past_key_values.0.{part}.{kv}" for part in ("decoder", "encoder") for kv in ("key", "value")]
        shape = ["batch_size", HEADS, "past_sequence_length", HEAD_DIM]
        return [SimpleNamespace(name=n, shape=shape) for n in names] + [SimpleNamespace(name="use_cache_branch", shape=[1])]

    def get_outputs(self):
        names = ["logits"] + [f"present.0.{part}.{kv}" for part in ("decoder", "encoder") for kv in ("key", "value")]
        return [SimpleNamespace(name=n) for n in names]

    def run(self, outputs, feed):
        self.passes += 1
        ids = feed["input_ids"][0].tolist()
        cached = bool(feed["use_cache_branch"][0])
        past = feed["past_key_values.0.decoder.key"]
        if cached:
            # The cache must hold exactly what was read before, and the
            # encoder's keys must be the first pass's, not a later empty one.
            assert past.shape[2] == len(self.history)
            assert feed["past_key_values.0.encoder.key"].shape[2] == 1500
            self.history += ids
        else:
            assert past.shape[2] == 0
            self.history = list(ids)
        logits = np.zeros((1, len(ids), VOCAB_SIZE), dtype=np.float32)
        for token, score in self.choose(self.history).items():
            logits[0, -1, token] = score
        decoder_kv = np.zeros((1, HEADS, len(self.history), HEAD_DIM), dtype=np.float32)
        encoder_kv = np.zeros((1, HEADS, 0 if cached else 1500, HEAD_DIM), dtype=np.float32)
        values = {"logits": logits}
        values.update({f"present.0.decoder.{kv}": decoder_kv for kv in ("key", "value")})
        values.update({f"present.0.encoder.{kv}": encoder_kv for kv in ("key", "value")})
        return [values[name] for name in outputs]


def _run(monkeypatch, tmp_path, choose, seconds=2.0):
    decoder = FakeDecoder(choose)
    sessions = {"encoder.onnx": FakeEncoder(), "decoder.onnx": decoder}
    monkeypatch.setattr(module.onnx_runtime, "load", lambda path: sessions[path])
    audio = np.zeros(int(16000 * seconds), dtype=np.float32)
    return module.transcribe(audio, _files(tmp_path)), decoder


def _scripted(history):
    """English; the best-scoring choice at each step is one the loop must refuse."""
    if history == [SOT]:
        return {EN: 5.0, ZH: 3.0}
    generated = history[4:]
    script = {
        0: {4: 9.0, EOS: 8.0, 0: 5.0},  # a leading space or an empty transcript is begin-suppressed
        1: {3: 9.0, 1: 5.0},  # a suppressed symbol
        2: {16: 9.0, 2: 5.0},  # a special token outside the text vocabulary
        3: {EOS: 5.0},
    }
    return script[len(generated)]


def test_transcribes_greedily_in_the_language_it_hears(monkeypatch, tmp_path):
    result, decoder = _run(monkeypatch, tmp_path, _scripted)
    assert result == module.Transcript(text="Hello there.", language="en", complete=True)
    assert decoder.history[:4] == [SOT, EN, TRANSCRIBE, NO_TIMESTAMPS]
    # One pass to hear the language, one for the prompt, one per token after.
    assert decoder.passes == 2 + 3


def test_a_transcript_that_never_ends_is_reported_incomplete(monkeypatch, tmp_path):
    def looping(history):
        return {EN: 1.0} if history == [SOT] else {0: 5.0}

    result, decoder = _run(monkeypatch, tmp_path, looping, seconds=1.0)
    assert not result.complete
    budget = int(module.TOKENS_PER_SECOND * 1.0) + module.TOKEN_SLACK
    assert len(decoder.history) == 4 + budget - 1  # the last token is chosen but never fed back


def test_more_than_thirty_seconds_is_refused(tmp_path):
    with pytest.raises(ValueError, match="30 s"):
        module.transcribe(np.zeros(16000 * 31, dtype=np.float32), _files(tmp_path))
