"""Turn an uploaded clip into the 32 integers that name a voice.

This is the whole of "cloning". There is no training step and no per-request
re-encoding: BiCodec's speaker encoder reads a six-second mel and emits 32 FSQ
codes, and those codes ARE the voice from then on.

The cheap part is that speaker identity comes from the mel ALONE. Upstream's
``BiCodec.tokenize`` looks like it needs the whole encoder stack, but its two
outputs have disjoint inputs — semantic tokens need wav2vec2 (1.2 GB) and the
BiCodec encoder (122 MB), while global tokens need only the mel and the 39 MB
of ECAPA + perceiver + FSQ that this ONNX graph contains. Computing them the
mel-only way was verified bit-identical to the full pipeline, and 260x faster.

Enrollment is also DETERMINISTIC: the same clip yields the same 32 ints across
runs and across processes. ``tests/unit/test_spark_enroll.py`` asserts that,
because it is what makes the stored tokens a reproduction record rather than a
snapshot of one lucky run.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import onnx_runtime
from . import audio_in, catalog, mel
from .tokens import SPEAKER_TOKEN_COUNT, validate_speaker_tokens

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)


class EnrollmentUnavailable(RuntimeError):
    """The codec half is missing, so no clip can be enrolled yet."""


def reference_clip(wav: np.ndarray) -> np.ndarray:
    """The exact six seconds the speaker encoder will read.

    Order matters and is upstream's: normalise the WHOLE clip, then take the
    window. Normalising the window instead measures the loudness of six seconds
    rather than of the recording, which moves the tokens.
    """
    return audio_in.reference_window(audio_in.volume_normalize(wav))


def enroll_window(window: np.ndarray) -> list[int]:
    """The 32 speaker tokens for a prepared :func:`reference_clip` window."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    ok, reason = catalog.codec_ready()
    if not ok:
        raise EnrollmentUnavailable(reason)
    spectrogram = mel.mel_spectrogram(window)
    session = onnx_runtime.load(catalog.speaker_encoder_path())
    # The graph takes (batch, n_mels, frames) — the transpose upstream does
    # inside `speaker_encoder.tokenize` is baked into the exported graph.
    raw = session.run(["speaker_tokens"], {"mel": spectrogram[None].astype(np.float32)})[0]
    tokens = [int(value) for value in np.asarray(raw).reshape(-1)]
    if len(tokens) != SPEAKER_TOKEN_COUNT:  # a re-export that changed token_num
        raise RuntimeError(f"speaker encoder returned {len(tokens)} tokens, expected {SPEAKER_TOKEN_COUNT}")
    return validate_speaker_tokens(tokens)


def enroll_signal(wav: np.ndarray) -> list[int]:
    """The 32 speaker tokens for an already-decoded 16 kHz mono signal."""
    return enroll_window(reference_clip(wav))


def enroll(data: bytes, *, filename: str = "") -> tuple[list[int], np.ndarray]:
    """``(speaker_tokens, reference_window)`` for an uploaded audio file.

    The window comes back so the caller can store the six seconds that were
    actually used — the identity is in the tokens, but keeping the audio means a
    model bump can re-enroll without asking the user for the file again, and it
    is what the "this is what we heard" preview plays.
    """
    window = reference_clip(audio_in.decode(data, filename=filename))
    return enroll_window(window), window


__all__ = ["EnrollmentUnavailable", "enroll", "enroll_signal", "enroll_window", "reference_clip"]
