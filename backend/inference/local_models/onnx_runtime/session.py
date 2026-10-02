"""Load and cache ONNX Runtime sessions for local-model artifacts."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import onnxruntime as ort

_SESSIONS: dict[str, Any] = {}
_ACTIVE = 0
_LEASE = threading.local()
_RELEASING = False
_LOCK = threading.Condition()  # sessions load off the event loop, from worker threads


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


def using(fn):
    """Lease the runtime for the full inference call, including session loads."""

    @wraps(fn)
    def leased(*args, **kwargs):
        global _ACTIVE
        outer = getattr(_LEASE, "depth", 0) == 0
        if outer:
            with _LOCK:
                if _RELEASING:
                    raise RuntimeError("Local model is being released")
                _ACTIVE += 1
        _LEASE.depth = getattr(_LEASE, "depth", 0) + 1
        try:
            return fn(*args, **kwargs)
        finally:
            _LEASE.depth -= 1
            if outer:
                with _LOCK:
                    _ACTIVE -= 1
                    _LOCK.notify_all()

    return leased


@contextmanager
def exclusive_release(timeout: float = 15.0, path: str | None = None):
    """Block new use until a file mutation completes; refuse an incomplete drain."""
    global _RELEASING
    with _LOCK:
        if _RELEASING:
            raise TimeoutError("Local model maintenance is already running")
        _RELEASING = True
    try:
        deadline = time.monotonic() + timeout
        with _LOCK:
            while _ACTIVE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Local model is still in use")
                _LOCK.wait(remaining)
            if path is None:
                _SESSIONS.clear()
            else:
                _SESSIONS.pop(os.path.normpath(path), None)
        yield
    finally:
        with _LOCK:
            _RELEASING = False
            _LOCK.notify_all()


def release(path: str | None = None) -> None:
    with exclusive_release(path=path):
        pass


__all__ = ["load", "release", "runtime_ok", "using", "exclusive_release"]
