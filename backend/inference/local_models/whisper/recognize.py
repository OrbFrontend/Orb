"""Transcribe a short clip with Whisper's ONNX export, greedily."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import onnx_runtime
from .features import SAMPLE_RATE, log_mel
from .vocab import Vocabulary

if TYPE_CHECKING:
    import numpy as np

#: Longest clip one pass reads; Whisper's window.
MAX_SECONDS = 30

#: Generation allowance per second of audio. Fast English is about six tokens
#: a second and Chinese can reach ten, so a transcript that needs more than
#: this is a decoding loop, not speech.
TOKENS_PER_SECOND = 16
TOKEN_SLACK = 16


@dataclass(frozen=True)
class WhisperFiles:
    """Paths for one Whisper checkpoint."""

    encoder: str
    decoder: str
    vocab: str
    config: str
    generation_config: str


@dataclass(frozen=True)
class Transcript:
    text: str
    #: False when generation hit its budget instead of ending the transcript.
    complete: bool


def _read_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class _Decoder:
    """Greedy steps through the merged decoder, carrying its key/value cache."""

    def __init__(self, session: Any, encoder_hidden: np.ndarray, config: dict[str, Any]) -> None:
        self._session = session
        self._hidden = encoder_hidden
        self._inputs = {spec.name: spec for spec in session.get_inputs()}
        self._outputs = [spec.name for spec in session.get_outputs()]
        heads = int(config["decoder_attention_heads"])
        self._head_dim = int(config["d_model"]) // heads
        self._heads = heads
        self._past: dict[str, np.ndarray] = {}

    def _empty_past(self, name: str) -> np.ndarray:
        import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

        shape = self._inputs[name].shape
        heads = shape[1] if isinstance(shape[1], int) else self._heads
        dim = shape[3] if isinstance(shape[3], int) else self._head_dim
        return np.zeros((1, heads, 0, dim), dtype=np.float32)

    def _run(self, ids: list[int], *, cached: bool) -> np.ndarray:
        import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

        feed: dict[str, np.ndarray] = {
            "input_ids": np.asarray([ids], dtype=np.int64),
            "encoder_hidden_states": self._hidden,
        }
        if "use_cache_branch" in self._inputs:
            feed["use_cache_branch"] = np.asarray([cached])
        for name in self._inputs:
            if name.startswith("past_key_values."):
                feed[name] = self._past[name] if cached else self._empty_past(name)
        values = dict(zip(self._outputs, self._session.run(self._outputs, feed), strict=True))
        for name, value in values.items():
            if not name.startswith("present."):
                continue
            # Encoder keys and values are only returned on the first pass.
            if cached and ".encoder." in name:
                continue
            self._past["past_key_values." + name.removeprefix("present.")] = value
        return values["logits"][0, -1]

    def start(self, ids: list[int]) -> np.ndarray:
        """Logits after reading *ids* from an empty cache."""
        self._past.clear()
        return self._run(ids, cached=False)

    def step(self, token: int) -> np.ndarray:
        """Logits after appending one token."""
        return self._run([token], cached=True)


def transcribe(wav: np.ndarray, files: WhisperFiles) -> Transcript:
    """Transcribe up to 30 s of 16 kHz mono audio."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    if audio.size > MAX_SECONDS * SAMPLE_RATE:
        raise ValueError(f"Whisper reads at most {MAX_SECONDS} s at a time")
    config = _read_json(files.config)
    generation = _read_json(files.generation_config)
    vocabulary = Vocabulary.load(files.vocab)

    features = log_mel(audio, int(config["num_mel_bins"]))
    hidden = np.asarray(onnx_runtime.load(files.encoder).run(None, {"input_features": features[None]})[0], dtype=np.float32)
    decoder = _Decoder(onnx_runtime.load(files.decoder), hidden, config)

    start = int(generation["decoder_start_token_id"])
    eos = int(generation["eos_token_id"])
    languages = {token.strip("<|>"): int(index) for token, index in generation["lang_to_id"].items()}
    heard = decoder.start([start])
    language_id = max(languages.values(), key=lambda index: heard[index])
    prompt = [
        start,
        language_id,
        int(generation["task_to_id"]["transcribe"]),
        int(generation["no_timestamps_token_id"]),
    ]

    # Only text tokens and end-of-text may be generated: a special token in
    # the middle of a transcript is a decoding failure, not content.
    allowed = np.full(int(config["vocab_size"]), -np.inf, dtype=np.float32)
    allowed[sorted(vocabulary.ids)] = 0.0
    allowed[[int(t) for t in generation.get("suppress_tokens") or []]] = -np.inf
    opening = allowed.copy()
    opening[[int(t) for t in generation.get("begin_suppress_tokens") or []]] = -np.inf

    budget = min(
        int(generation.get("max_length") or config["max_target_positions"]) - len(prompt),
        int(TOKENS_PER_SECOND * audio.size / SAMPLE_RATE) + TOKEN_SLACK,
    )
    logits = decoder.start(prompt)
    generated: list[int] = []
    complete = False
    for step in range(budget):
        token = int(np.argmax(logits + (opening if step == 0 else allowed)))
        if token == eos:
            complete = True
            break
        generated.append(token)
        if step + 1 < budget:
            logits = decoder.step(token)
    text = " ".join(vocabulary.decode(generated, skip=(eos,)).split())
    return Transcript(text=text, complete=complete)


def release(files: WhisperFiles) -> None:
    """Release the encoder and decoder sessions."""
    onnx_runtime.release(files.encoder)
    onnx_runtime.release(files.decoder)


__all__ = ["MAX_SECONDS", "Transcript", "WhisperFiles", "release", "transcribe"]
