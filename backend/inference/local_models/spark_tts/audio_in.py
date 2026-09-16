"""Turn an uploaded audio file into the 16 kHz mono signal enrollment needs.

Orb's base dependencies are FastAPI/Pillow/httpx — no soundfile, no torchaudio,
no scipy — so this module is deliberately layered from what is certainly there
to what merely might be:

1. the stdlib ``wave`` module, which reads PCM WAV and nothing else;
2. ``soundfile`` when it is installed, which adds FLAC/OGG and the WAV
   encodings ``wave`` rejects;
3. ``ffmpeg`` when it happens to be on PATH, which covers MP3/M4A — the format
   a phone recording actually arrives in.

A file none of the three can read raises :class:`UnsupportedAudio`, whose
message names what would have read it. That is a decision the user can act on;
"could not decode" is not.

Nothing here loads a model, so it is all unit-testable, and it is: resampling
and volume normalisation both move the 32 speaker tokens, which makes them
correctness surface rather than convenience.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import subprocess  # noqa: S404 — ffmpeg, argv-only, never a shell string
import tempfile
import wave
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy arrives with onnxruntime; base Orb must import this module without it
    import numpy as np

logger = logging.getLogger(__name__)

TARGET_RATE = 16000

#: Longest source clip we will decode, and therefore the most audio one voice is
#: enrolled from (see :func:`reference_signal`). Past two minutes the voice has
#: long since stopped moving, and an uncapped decode is a memory budget set by
#: whoever picks the file.
MAX_SOURCE_SECONDS = 120

#: Zero crossings each side of the resampling kernel. 16 is the usual
#: "good enough for anything but mastering" figure, and enrollment is a
#: one-shot on at most two minutes of speech.
_SINC_ZEROS = 16


class UnsupportedAudio(ValueError):
    """The upload could not be decoded by any reader this install has."""


def ffmpeg_path() -> str | None:
    """``ffmpeg`` on PATH, or ``None``. Probed, never assumed."""
    return shutil.which("ffmpeg")


def _have_soundfile() -> bool:
    try:
        import soundfile  # noqa: F401, PLC0415 — deferred probe
    except Exception:
        return False
    return True


def _from_wave(data: bytes) -> tuple[np.ndarray, int] | None:
    """Decode PCM WAV with the stdlib. ``None`` when this is not that."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = min(handle.getnframes(), rate * MAX_SOURCE_SECONDS)
            raw = handle.readframes(frames)
    except (wave.Error, EOFError):
        return None
    if not raw or channels < 1 or rate <= 0:
        return None
    if width == 3:
        # 24-bit has no numpy dtype and `audioop`, which used to widen it, was
        # removed in Python 3.13 — so the three little-endian bytes become the
        # high three of an int32 by construction, which also sign-extends.
        packed = np.frombuffer(raw[: len(raw) // 3 * 3], dtype=np.uint8).reshape(-1, 3)
        widened = np.zeros((packed.shape[0], 4), dtype=np.uint8)
        widened[:, 1:] = packed
        samples = widened.view("<i4").reshape(-1).astype(np.float32)
        width = 4
    else:
        dtype = {1: np.uint8, 2: "<i2", 4: "<i4"}.get(width)
        if dtype is None:
            return None
        samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if width == 1:  # 8-bit WAV is unsigned, centred on 128
        samples = (samples - 128.0) / 128.0
    else:
        samples /= float(1 << (8 * width - 1))
    return _to_mono(samples, channels), rate


def _from_soundfile(data: bytes) -> tuple[np.ndarray, int] | None:
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    try:
        import soundfile  # noqa: PLC0415 — deferred; optional dependency
    except Exception:
        return None
    try:
        with soundfile.SoundFile(io.BytesIO(data)) as handle:
            rate = int(handle.samplerate)
            block = handle.read(frames=rate * MAX_SOURCE_SECONDS, dtype="float32", always_2d=True)
    except Exception as exc:  # libsndfile says "format not recognised" for anything it lacks
        logger.debug("soundfile could not read the upload: %s", exc)
        return None
    if block.size == 0 or rate <= 0:
        return None
    return np.ascontiguousarray(block.mean(axis=1), dtype=np.float32), rate


def _from_ffmpeg(data: bytes, suffix: str) -> tuple[np.ndarray, int] | None:
    """Shell out to a user-supplied ffmpeg, which decodes everything else.

    Via a temp FILE, not a pipe: MP4/M4A carries its index at the end of the
    stream and ffmpeg has to seek back to it, which a pipe cannot do — the
    common case (a voice memo) is exactly the one that would fail.
    """
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    binary = ffmpeg_path()
    if binary is None:
        return None
    fd, path = tempfile.mkstemp(suffix=suffix or ".bin")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # Fixed argv. The one variable is a path this process just created, and
        # `-i` before it means a name beginning with `-` is still read as a file.
        argv = [
            binary, "-nostdin", "-v", "error",
            "-t", str(MAX_SOURCE_SECONDS),
            "-i", path,
            "-map", "a:0",  # the audio of a video file, and only the first track
            "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(TARGET_RATE),
            "pipe:1",
        ]  # fmt: skip
        try:
            done = subprocess.run(argv, capture_output=True, timeout=120, check=False)  # noqa: S603 — argv-only, binary from which()
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("ffmpeg failed to decode the upload: %s", exc)
            return None
        if done.returncode != 0 or not done.stdout:
            logger.warning("ffmpeg rejected the upload: %s", (done.stderr or b"").decode("utf-8", "replace")[-400:])
            return None
        # ffmpeg resampled for us, so this is already at TARGET_RATE.
        return np.frombuffer(done.stdout, dtype=np.float32).copy(), TARGET_RATE
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _to_mono(interleaved: np.ndarray, channels: int) -> np.ndarray:
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    if channels <= 1:
        return np.ascontiguousarray(interleaved, dtype=np.float32)
    usable = (len(interleaved) // channels) * channels
    return np.ascontiguousarray(interleaved[:usable].reshape(-1, channels).mean(axis=1), dtype=np.float32)


def resample(wav: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    """Band-limited resample of a mono signal.

    A windowed sinc evaluated directly at the output positions, rather than
    scipy's polyphase — scipy is 30 MB for one call per uploaded clip, on
    a code path whose entire purpose is not installing large packages. The
    kernel is Kaiser-windowed and its cutoff drops with the rate ratio, so
    downsampling is anti-aliased rather than aliased-then-decimated.
    """
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    if src_rate == dst_rate or wav.size == 0:
        return np.ascontiguousarray(wav, dtype=np.float32)
    ratio = dst_rate / float(src_rate)
    # Downsampling: cut at the OUTPUT Nyquist, in input-rate units, with a
    # little guard band. Upsampling needs no extra band limiting.
    cutoff = min(1.0, ratio) * 0.95
    half = int(np.ceil(_SINC_ZEROS / cutoff))
    taps = np.arange(-half + 1, half + 1, dtype=np.float64)
    out_len = max(1, int(np.floor(wav.size * ratio)))
    source = np.asarray(wav, dtype=np.float64)
    out = np.empty(out_len, dtype=np.float64)
    # Chunked so the (out_len x 2*half) weight matrix stays a few MB rather
    # than scaling with the length of whatever the user uploaded.
    block = 8192
    for start in range(0, out_len, block):
        stop = min(start + block, out_len)
        centre = np.arange(start, stop, dtype=np.float64) / ratio
        base = np.floor(centre).astype(np.int64)
        offsets = base[:, None] + taps[None, :]
        delta = offsets - centre[:, None]
        weights = cutoff * np.sinc(cutoff * delta) * np.kaiser(2 * half, 5.0)[None, :]
        # Edge samples read past the ends; clamping the index and zeroing the
        # weight is the same as zero-padding, without materialising the pad.
        inside = (offsets >= 0) & (offsets < source.size)
        gathered = source[np.clip(offsets, 0, source.size - 1).astype(np.int64)]
        out[start:stop] = np.einsum("ij,ij->i", gathered * inside, weights)
    return out.astype(np.float32)


def volume_normalize(wav: np.ndarray, coeff: float = 0.2) -> np.ndarray:
    """Upstream's ``audio_volume_normalize``, reproduced exactly.

    It is not cosmetic: the mel it feeds is what the speaker encoder quantises,
    so a different gain is a different set of 32 tokens. Kept faithful to
    ``sparktts/utils/audio.py`` rather than replaced with a peak or RMS
    normaliser that would be tidier and wrong.
    """
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    audio = np.asarray(wav, dtype=np.float32).reshape(-1).copy()
    if audio.size == 0:
        return audio
    temp = np.sort(np.abs(audio))
    if temp[-1] < 0.1:  # a very quiet recording is lifted before anything else
        audio = audio / max(float(temp[-1]), 1e-3) * 0.1
    temp = temp[temp > 0.01]
    if temp.shape[0] <= 10:  # too little signal to measure; leave it alone
        return audio
    length = temp.shape[0]
    volume = float(np.mean(temp[int(0.9 * length) : int(0.99 * length)]))
    audio = audio * np.clip(coeff / volume, 0.1, 10)
    peak = float(np.max(np.abs(audio)))
    if peak > 1:
        audio = audio / peak
    return audio.astype(np.float32)


def decode(data: bytes, *, filename: str = "") -> np.ndarray:
    """An uploaded file's audio as 16 kHz mono float32.

    Raises :class:`UnsupportedAudio` when no reader on this machine can open
    it, naming the ones that would have.
    """
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    if not data:
        raise UnsupportedAudio("The uploaded file is empty.")
    suffix = os.path.splitext(filename)[1].lower()
    for reader in (_from_wave, _from_soundfile):
        got = reader(data)
        if got is not None:
            signal, rate = got
            break
    else:
        got = _from_ffmpeg(data, suffix)
        if got is None:
            raise UnsupportedAudio(_unreadable_message(suffix))
        signal, rate = got
    if signal.size == 0:
        raise UnsupportedAudio("The uploaded file contains no audio.")
    signal = signal[: rate * MAX_SOURCE_SECONDS]
    resampled = resample(signal, rate, TARGET_RATE)
    # An upload can be silent, or clipped well past full scale; neither should
    # reach the mel as inf/NaN.
    return np.nan_to_num(resampled, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)


def _unreadable_message(suffix: str) -> str:
    kind = suffix.lstrip(".").upper() or "That file"
    if _have_soundfile():
        return f"{kind} audio needs ffmpeg, which is not on PATH. Install ffmpeg, or upload a WAV or FLAC file."
    return f"{kind} audio could not be read. Upload a PCM WAV file, or install ffmpeg to accept any format."


def reference_signal(wav: np.ndarray, min_seconds: int = 6, hop: int = 320) -> np.ndarray:
    """What the speaker encoder reads: the whole clip, never less than six seconds.

    A clip shorter than Spark-TTS's ``ref_segment_duration`` is REPEATED to fill
    it, as upstream does, rather than zero-padded — silence would be seconds of
    a speaker who is not speaking.

    A longer clip is kept WHOLE, which is where this departs from upstream.
    Upstream crops to the first six seconds, and which six seconds turns out to
    matter: across the 6 s windows of one 47 s recording, an independent
    speaker-verification model (WavLM-SV) scored the clones 0.87–0.94 against
    the real voice, and the first window was the worst of them. The whole clip
    in one pass scored 0.93 with no run-on generations. Length does not rescue
    a noisy recording: 120 s with music under the voice scored 0.88 whole or
    cropped. The encoder takes any frame count, so this is a longer input, not
    a different model. Upstream's tokens are still reproduced exactly for
    anything six seconds or shorter.
    """
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    minimum = int(TARGET_RATE * min_seconds) // hop * hop
    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return np.zeros(minimum, dtype=np.float32)
    if audio.size < minimum:
        return np.ascontiguousarray(np.tile(audio, minimum // audio.size + 1)[:minimum], dtype=np.float32)
    return np.ascontiguousarray(audio[: audio.size // hop * hop], dtype=np.float32)


__all__ = [
    "MAX_SOURCE_SECONDS",
    "TARGET_RATE",
    "UnsupportedAudio",
    "decode",
    "ffmpeg_path",
    "reference_signal",
    "resample",
    "volume_normalize",
]
