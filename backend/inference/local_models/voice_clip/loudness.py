"""Integrated loudness (ITU-R BS.1770) and the level of a clip's quietest moments."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

_BLOCK_SECONDS = 0.4
_ABSOLUTE_GATE = -70.0
_RELATIVE_GATE = -10.0


def _biquad_response(b: tuple[float, float, float], a: tuple[float, float, float], w: np.ndarray) -> np.ndarray:
    import numpy as np  # noqa: PLC0415 -- deferred; numpy is an optional runtime dependency

    z1, z2 = np.exp(-1j * w), np.exp(-2j * w)
    return (b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2)


def _k_weighting(rate: int, w: np.ndarray) -> np.ndarray:
    """The K-weighting filter's response at angular frequencies *w*, its two stages derived for *rate*."""
    w0 = 2 * math.pi * 1500.0 / rate
    gain = 10 ** (4.0 / 40)
    alpha = math.sin(w0) / (2 / math.sqrt(2))
    cos, root = math.cos(w0), 2 * math.sqrt(gain) * alpha
    shelf = _biquad_response(
        (
            gain * ((gain + 1) + (gain - 1) * cos + root),
            -2 * gain * ((gain - 1) + (gain + 1) * cos),
            gain * ((gain + 1) + (gain - 1) * cos - root),
        ),
        ((gain + 1) - (gain - 1) * cos + root, 2 * ((gain - 1) - (gain + 1) * cos), (gain + 1) - (gain - 1) * cos - root),
        w,
    )
    w0 = 2 * math.pi * 38.0 / rate
    alpha, cos = math.sin(w0) / (2 * 0.5), math.cos(w0)
    high_pass = _biquad_response(((1 + cos) / 2, -(1 + cos), (1 + cos) / 2), (1 + alpha, -2 * cos, 1 - alpha), w)
    return shelf * high_pass


def integrated_lufs(signal: np.ndarray, rate: int) -> float:
    """Gated integrated loudness of a mono signal in LUFS; ``-inf`` when nothing passes the absolute gate."""
    import numpy as np  # noqa: PLC0415 -- deferred; numpy is an optional runtime dependency

    x = np.asarray(signal, dtype=np.float64).reshape(-1)
    block = int(_BLOCK_SECONDS * rate)
    if x.size < block:
        return -math.inf
    size = 1 << int(math.ceil(math.log2(x.size + rate)))
    spectrum = np.fft.rfft(x, size) * _k_weighting(rate, np.linspace(0, math.pi, size // 2 + 1))
    weighted = np.fft.irfft(spectrum, size)[: x.size]
    step = block // 4
    power = np.array([np.mean(weighted[i : i + block] ** 2) for i in range(0, x.size - block + 1, step)])
    loud = power[-0.691 + 10 * np.log10(power + 1e-20) > _ABSOLUTE_GATE]
    if loud.size == 0:
        return -math.inf
    gate = -0.691 + 10 * math.log10(float(loud.mean())) + _RELATIVE_GATE
    kept = loud[-0.691 + 10 * np.log10(loud) > gate]
    return -0.691 + 10 * math.log10(float(kept.mean()))


def floor_db(signal: np.ndarray, rate: int) -> float:
    """How far the clip's quietest moments sit below its speech, in dB: the 5th against the 90th percentile of 20 ms levels.

    A clean recording drops 60 dB or more between phrases; music or room noise under the voice keeps it within about 40.
    """
    import numpy as np  # noqa: PLC0415 -- deferred; numpy is an optional runtime dependency

    x = np.asarray(signal, dtype=np.float64).reshape(-1)
    window = max(1, int(rate * 0.02))
    frames = x[: x.size // window * window].reshape(-1, window)
    if frames.shape[0] < 20:
        return -math.inf
    levels = 10 * np.log10(np.mean(frames**2, axis=1) + 1e-20)
    return float(np.percentile(levels, 5) - np.percentile(levels, 90))
