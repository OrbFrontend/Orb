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
