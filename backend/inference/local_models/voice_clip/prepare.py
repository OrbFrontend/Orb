"""Turn an uploaded clip into the reference ZONOS3 clones from: its first minute, vocals only when needed, at a fixed level."""

from __future__ import annotations

import io
import math
import wave
from dataclasses import dataclass

from ..spark_tts.audio_in import UnsupportedAudio, decode_native, resample
from . import loudness, separator

SAMPLE_RATE = separator.SAMPLE_RATE
#: ZONOS3 keeps the first 60 s of a reference.
REFERENCE_SECONDS = 60
TARGET_LUFS = -20.0
_PEAK_CEILING = 10 ** (-1.0 / 20)
#: A quietest-moment level above this, relative to speech, means something plays under the voice.
BACKGROUND_FLOOR_DB = -45.0


@dataclass(frozen=True)
class PreparedClip:
    """A 16-bit mono WAV ready to upload, and what was done to it."""

    wav: bytes
    background: bool
    separated: bool


def _wav_bytes(signal) -> bytes:
    import numpy as np  # noqa: PLC0415 -- deferred; numpy is an optional runtime dependency

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes((np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


def prepare(data: bytes, *, filename: str = "", separate: bool) -> PreparedClip:
    """Decode *data*, keep its first ``REFERENCE_SECONDS``, separate its vocals when *separate* and something plays under the
    voice, and set it to ``TARGET_LUFS`` with one gain that keeps peaks below -1 dBFS.

    Raises ``UnsupportedAudio`` for a clip that cannot be read or holds no speech.
    """
    import numpy as np  # noqa: PLC0415 -- deferred; numpy is an optional runtime dependency

    signal, rate = decode_native(data, filename=filename, ffmpeg_rate=SAMPLE_RATE)
    signal = resample(signal, rate, SAMPLE_RATE)[: SAMPLE_RATE * REFERENCE_SECONDS]
    signal = np.nan_to_num(signal, nan=0.0, posinf=1.0, neginf=-1.0)
    background = loudness.floor_db(signal, SAMPLE_RATE) > BACKGROUND_FLOOR_DB
    separated = background and separate
    if separated:
        signal = separator.vocals(signal)
    level = loudness.integrated_lufs(signal, SAMPLE_RATE)
    if not math.isfinite(level):
        raise UnsupportedAudio("The clip has no speech loud enough to clone from.")
    peak = float(np.abs(signal).max())
    gain = min(10 ** ((TARGET_LUFS - level) / 20), _PEAK_CEILING / peak if peak > 0 else math.inf)
    return PreparedClip(_wav_bytes(signal * gain), background, separated)
