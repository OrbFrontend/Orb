"""The ``onnx`` runtime slice: provider selection and the session cache.

Small, but both halves have a failure mode worth pinning. A provider list that
drops CPU is a model that will not load at all on a machine missing the named
accelerator; a cache that never releases is a 385 MB graph holding a file open,
which on Windows makes deleting the model fail outright.
"""

from __future__ import annotations

import pytest

from backend.inference.local_models import onnx_runtime
from backend.inference.local_models.onnx_runtime import session


def test_cpu_is_always_the_last_resort(monkeypatch):
    """A named provider this build does not have must degrade to CPU rather
    than fail to load the model."""
    monkeypatch.setenv(session._EP_ENV, "CoreMLExecutionProvider,NotARealProvider")
    monkeypatch.setattr(session, "available_providers", lambda: ["CoreMLExecutionProvider", "CPUExecutionProvider"])
    assert session.preferred_providers() == ["CoreMLExecutionProvider", "CPUExecutionProvider"]

    monkeypatch.setattr(session, "available_providers", lambda: ["CPUExecutionProvider"])
    assert session.preferred_providers() == ["CPUExecutionProvider"]


def test_cpu_only_by_default(monkeypatch):
    """CPU is the target: ONNX Runtime has no Vulkan provider, Orb's Vulkan
    belongs to llama.cpp, and an accelerator that changes numerics on some
    machines is opted into by name."""
    monkeypatch.delenv(session._EP_ENV, raising=False)
    monkeypatch.setattr(session, "available_providers", lambda: ["CoreMLExecutionProvider", "CPUExecutionProvider"])
    assert session.preferred_providers() == ["CPUExecutionProvider"]


def test_loading_a_missing_model_names_the_path(monkeypatch, tmp_path):
    pytest.importorskip("onnxruntime")
    with pytest.raises(FileNotFoundError, match="gone.onnx"):
        onnx_runtime.load(str(tmp_path / "gone.onnx"))


def test_release_drops_cached_sessions(monkeypatch):
    """What runs before a model file is deleted or replaced."""
    monkeypatch.setattr(session, "_SESSIONS", {"/a.onnx": object(), "/b.onnx": object()})
    assert set(session.loaded_paths()) == {"/a.onnx", "/b.onnx"}
    onnx_runtime.release("/a.onnx")
    assert session.loaded_paths() == ("/b.onnx",)
    onnx_runtime.release()
    assert session.loaded_paths() == ()


def test_releasing_something_never_loaded_is_not_an_error():
    onnx_runtime.release("/never/loaded.onnx")
