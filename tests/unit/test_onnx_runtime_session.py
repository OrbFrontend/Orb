"""The ``onnx`` runtime slice: a small session cache.

The cache must release its 385 MB graph before the model file is deleted.
"""

from __future__ import annotations

import pytest

from backend.inference.local_models import onnx_runtime
from backend.inference.local_models.onnx_runtime import session


def test_loading_a_missing_model_names_the_path(tmp_path):
    pytest.importorskip("onnxruntime")
    with pytest.raises(FileNotFoundError, match="gone.onnx"):
        onnx_runtime.load(str(tmp_path / "gone.onnx"))


def test_release_drops_cached_sessions(monkeypatch):
    """What runs before a model file is deleted or replaced."""
    monkeypatch.setattr(session, "_SESSIONS", {"/a.onnx": object(), "/b.onnx": object()})
    onnx_runtime.release("/a.onnx")
    assert set(session._SESSIONS) == {"/b.onnx"}
    onnx_runtime.release()
    assert not session._SESSIONS


def test_releasing_something_never_loaded_is_not_an_error():
    onnx_runtime.release("/never/loaded.onnx")


def test_active_lease_refuses_release_and_keeps_cached_session():
    import threading

    entered, release = threading.Event(), threading.Event()
    cached = object()
    session._SESSIONS["leased.onnx"] = cached

    @session.using
    def infer():
        entered.set()
        assert release.wait(5)

    thread = threading.Thread(target=infer)
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(TimeoutError):
            with session.exclusive_release(timeout=0):
                pytest.fail("must not permit file deletion")
        assert session._SESSIONS["leased.onnx"] is cached
    finally:
        release.set()
        thread.join(5)
    with session.exclusive_release(timeout=0):
        assert not session._SESSIONS


def test_cache_release_never_waits_for_running_inference():
    """Feature toggles and enrollment cleanup drop the cache; only a file delete drains."""
    import threading

    entered, finish = threading.Event(), threading.Event()
    session._SESSIONS["busy.onnx"] = object()

    @session.using
    def infer():
        entered.set()
        assert finish.wait(5)

    thread = threading.Thread(target=infer)
    thread.start()
    assert entered.wait(5)
    try:
        onnx_runtime.release("busy.onnx")
        assert "busy.onnx" not in session._SESSIONS
    finally:
        finish.set()
        thread.join(5)
