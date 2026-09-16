"""Decode Spark-TTS semantic tokens into PCM audio."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .. import onnx_runtime
from . import catalog
from .tokens import SEMANTIC_COUNT, validate_speaker_tokens

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class EmptyGeneration(RuntimeError):
    """The model produced no semantic tokens."""


def decode(semantic: Sequence[int], speaker_tokens: Sequence[int]) -> bytes:
    """Decode semantic and speaker tokens to 16-bit mono PCM."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    if not semantic:
        raise EmptyGeneration("Spark-TTS produced no audio tokens for this line.")
    speaker = validate_speaker_tokens(list(speaker_tokens))
    indices = np.asarray(semantic, dtype=np.int64)
    if indices.min() < 0 or indices.max() >= SEMANTIC_COUNT:  # a caller that skipped the range filter
        raise ValueError("semantic token indices are outside the codebook")
    session = onnx_runtime.load(catalog.decoder_path())
    audio = session.run(
        ["audio"],
        {
            "semantic_tokens": indices[None, :],
            # The decoder expects speaker tokens with an extra axis.
            "global_tokens": np.asarray(speaker, dtype=np.int64)[None, None, :],
        },
    )[0]
    return to_pcm16(np.asarray(audio).reshape(-1))


def to_pcm16(audio) -> bytes:
    """Encode a float waveform as 16-bit little-endian PCM."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    samples = np.nan_to_num(np.asarray(audio, dtype=np.float32).reshape(-1), copy=False)
    if samples.size == 0:
        return b""
    peak = float(np.max(np.abs(samples)))
    if peak > 1.0:
        samples = samples / peak
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


__all__ = ["SAMPLE_RATE", "EmptyGeneration", "decode", "to_pcm16"]
