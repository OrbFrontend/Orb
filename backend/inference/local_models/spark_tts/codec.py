"""Render Spark-TTS audio tokens back into a waveform.

``bicodec.onnx`` is the decode half of BiCodec — ``quantizer.detokenize``,
``speaker_encoder.detokenize``, ``prenet`` and ``decoder``, and none of the
encoder, postnet or mel transform, which is why it is 385 MB against the
checkpoint's 625 MB. It reproduces the torch decoder at 114 dB SNR, holding
from one semantic token to the 1500 at the generation cap.

It is not a speed win — ONNX Runtime measured slightly SLOWER than torch here
(0.84x) — and it is not meant to be. It is how the decoder ships without torch,
and at 0.22x realtime on CPU the vocoder is not the constraint: the LLM is,
which is the half that already runs on Orb's llama-server.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .. import onnx_runtime
from . import catalog
from .tokens import SEMANTIC_COUNT, validate_speaker_tokens

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class EmptyGeneration(RuntimeError):
    """The model produced no semantic tokens; there is nothing to render.

    Kept distinct so a caller can retry rather than report a broken install.
    Upstream hits this as a crash deep inside einx on a zero-length axis.
    """


def decode(semantic: Sequence[int], speaker_tokens: Sequence[int]) -> bytes:
    """``(semantic indices, 32 speaker tokens)`` -> 16-bit mono PCM at 16 kHz.

    Raw PCM rather than a WAV file because the TTS workflow stitches per-chunk
    clips with real silence between them and wraps the result once, in
    ``engine/wav.py``.
    """
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
            # (batch, 1, 32): the decoder's own extra axis, not a mistake.
            "global_tokens": np.asarray(speaker, dtype=np.int64)[None, None, :],
        },
    )[0]
    return to_pcm16(np.asarray(audio).reshape(-1))


def to_pcm16(audio) -> bytes:
    """Encode a float waveform as 16-bit little-endian PCM.

    Spark-TTS returns float32 nominally in [-1, 1] but a degenerate generation
    can overshoot; peak-normalise those rather than let the clipping reach the
    listener.
    """
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    samples = np.nan_to_num(np.asarray(audio, dtype=np.float32).reshape(-1), copy=False)
    if samples.size == 0:
        return b""
    peak = float(np.max(np.abs(samples)))
    if peak > 1.0:
        samples = samples / peak
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


__all__ = ["SAMPLE_RATE", "EmptyGeneration", "decode", "to_pcm16"]
