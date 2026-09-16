"""Load and cache ONNX Runtime sessions for local-model artifacts.

The ``llama_server`` sibling supervises a child process; this is the whole
equivalent for the ``onnx`` runtime, because there is no process to supervise —
a session is a handle on a memory-mapped graph. What it still needs is the two
things the llama-server slice provides: ONE place that decides a model is
loadable, and ONE place that lets go of a file so it can be deleted or replaced.

Sessions are cached per path and never reloaded implicitly. The bicodec decoder
is 385 MB and Orb's TTS is bursty — reloading it per line would dominate a
synthesis that otherwise runs at 0.22x realtime.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import onnxruntime as ort

logger = logging.getLogger(__name__)

#: Opt-in accelerated execution providers, tried ahead of CPU when named.
#: CPU is the target: ONNX Runtime has NO Vulkan EP, Orb's Vulkan belongs to
#: llama.cpp and lands on the LLM (63% of synthesis wall time), and the vocoder
#: measured at 0.22x realtime on CPU. CoreML and DirectML are therefore an
#: experiment a user opts into by name, not a default that changes numerics on
#: some machines and not others.
_EP_ENV = "ORB_ONNX_PROVIDERS"

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


def available_providers() -> list[str]:
    """Execution providers this install actually has, or ``[]`` with no runtime."""
    try:
        import onnxruntime as ort  # noqa: PLC0415 — deferred; see runtime_ok
    except Exception:
        return []
    return list(ort.get_available_providers())


def preferred_providers() -> list[str]:
    """The provider list a session is opened with.

    CPU is always last and always present, so a named provider that turns out
    to be missing degrades to CPU instead of failing to load the model.
    """
    have = set(available_providers())
    wanted = [name.strip() for name in os.environ.get(_EP_ENV, "").split(",") if name.strip()]
    chosen = [name for name in wanted if name in have]
    for name in wanted:
        if name not in have:
            logger.warning("%s names %s, which this onnxruntime build does not have; ignoring it", _EP_ENV, name)
    return [*chosen, "CPUExecutionProvider"]


def load(path: str) -> ort.InferenceSession:
    """The cached session for *path*, loading it on first use.

    *path* is a trusted absolute path resolved by the caller's closed catalog,
    the same contract ``LaunchProfile.model_path`` carries.
    """
    import onnxruntime as ort  # noqa: PLC0415 — deferred; see runtime_ok

    key = os.path.normpath(path)
    with _LOCK:
        cached = _SESSIONS.get(key)
        if cached is not None:
            return cached
    if not os.path.exists(key):
        raise FileNotFoundError(f"ONNX model not found: {key}")
    options = ort.SessionOptions()
    # One local user, one clip at a time: the default thread pool sized to the
    # whole machine competes with the llama-server child for the same cores,
    # and that child is the actual bottleneck.
    options.log_severity_level = 3  # warnings and below are the loader's business
    session = ort.InferenceSession(key, sess_options=options, providers=preferred_providers())
    with _LOCK:
        # Another thread may have won the race; keep whichever landed first so
        # there is never more than one live session per path.
        return _SESSIONS.setdefault(key, session)


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


def loaded_paths() -> tuple[str, ...]:
    """Which models are resident. For status surfaces and tests."""
    with _LOCK:
        return tuple(_SESSIONS)


__all__ = ["available_providers", "load", "loaded_paths", "preferred_providers", "release", "runtime_ok"]
