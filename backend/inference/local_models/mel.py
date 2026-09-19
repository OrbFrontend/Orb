"""Shared Slaney mel-scale primitives for local audio model frontends."""

from __future__ import annotations


def hz_to_mel(freq):
    """Convert frequency in Hz to Slaney's mel scale."""
    import numpy as np  # noqa: PLC0415 — numpy is an optional ML dependency

    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    freq = np.asarray(freq, dtype=np.float64)
    safe = np.maximum(freq, min_log_hz)
    return np.where(freq >= min_log_hz, min_log_mel + np.log(safe / min_log_hz) / logstep, freq / f_sp)


def mel_to_hz(mels):
    """Convert Slaney mel values to frequency in Hz."""
    import numpy as np  # noqa: PLC0415 — numpy is an optional ML dependency

    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    mels = np.asarray(mels, dtype=np.float64)
    return np.where(mels >= min_log_mel, min_log_hz * np.exp(logstep * (mels - min_log_mel)), f_sp * mels)


__all__ = ["hz_to_mel", "mel_to_hz"]
