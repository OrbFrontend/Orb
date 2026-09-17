"""Create the 32 speaker tokens used by the Spark-TTS voice cloner."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import onnx_runtime
from . import audio_in, catalog, mel, silence
from .tokens import SPEAKER_TOKEN_COUNT, validate_speaker_tokens

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)


class EnrollmentUnavailable(RuntimeError):
    """The codec half is missing, so no clip can be enrolled yet."""


def reference_clip(wav: np.ndarray, *, trim: bool = True) -> np.ndarray:
    """Normalize and shape the signal before speaker encoding."""
    normalized = audio_in.volume_normalize(wav)
    if trim:
        normalized = silence.trim_silence(normalized)
    return audio_in.reference_signal(normalized)


def enroll_signal(signal: np.ndarray) -> list[int]:
    """The 32 speaker tokens for a prepared :func:`reference_clip` signal."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    ok, reason = catalog.codec_ready()
    if not ok:
        raise EnrollmentUnavailable(reason)
    spectrogram = mel.mel_spectrogram(signal)
    session = onnx_runtime.load(catalog.speaker_encoder_path())
    # The exported graph expects (batch, n_mels, frames).
    raw = session.run(["speaker_tokens"], {"mel": spectrogram[None].astype(np.float32)})[0]
    tokens = [int(value) for value in np.asarray(raw).reshape(-1)]
    if len(tokens) != SPEAKER_TOKEN_COUNT:  # a re-export that changed token_num
        raise RuntimeError(f"speaker encoder returned {len(tokens)} tokens, expected {SPEAKER_TOKEN_COUNT}")
    return validate_speaker_tokens(tokens)


def enroll(data: bytes, *, filename: str = "") -> list[int]:
    """Decode an upload and return its speaker tokens."""
    return enroll_signal(reference_clip(audio_in.decode(data, filename=filename)))


__all__ = ["EnrollmentUnavailable", "enroll", "enroll_signal", "reference_clip"]
