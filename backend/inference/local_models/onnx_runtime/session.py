"""Load and cache ONNX Runtime sessions for local-model artifacts."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import onnxruntime as ort

_SESSIONS: dict[str, Any] = {}
_LOCK = threading.Lock()  # sessions load off the event loop, from worker threads


def runtime_ok() -> bool:
    """Is ``onnxruntime`` importable? The ``onnx`` package is NOT needed —
    that one only *builds* graphs and belongs in requirements-dev.txt."""
    try:
        import onnxruntime  # noqa: F401, PLC0415 — deferred; base Orb has no ML extras
    except Exception:
        return False
    return True


def load(path: str) -> ort.InferenceSession:
    """The cached session for *path*, loading it on first use.

    *path* is a trusted absolute path resolved by the caller's closed catalog,
    the same contract ``LaunchProfile.model_path`` carries.
    """
    import onnxruntime as ort  # noqa: PLC0415 — deferred; see runtime_ok

    key = os.path.normpath(path)
    if not os.path.exists(key):
        raise FileNotFoundError(f"ONNX model not found: {key}")
    with _LOCK:
        if key not in _SESSIONS:
            options = ort.SessionOptions()
            options.log_severity_level = 3
            _SESSIONS[key] = ort.InferenceSession(key, sess_options=options, providers=["CPUExecutionProvider"])
        return _SESSIONS[key]


def release(path: str | None = None) -> None:
    """Drop cached sessions so their files can be deleted or replaced.

    Called before a model delete for the same reason the llama-server host is
    released first: on Windows an open handle makes the unlink fail outright,
    and everywhere else it leaves the old graph resident.
    """
    with _LOCK:
        if path is None:
            _SESSIONS.clear()
            return
        _SESSIONS.pop(os.path.normpath(path), None)


__all__ = ["load", "release", "runtime_ok"]
