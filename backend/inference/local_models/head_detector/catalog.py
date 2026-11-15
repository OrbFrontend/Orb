"""Locate the head detector and report its readiness."""

from __future__ import annotations

from .. import assets, dependencies, onnx_runtime

FEATURE = "head_detector"


def model_path() -> str:
    """The detector graph's absolute path (may not exist)."""
    return assets.resolve_path(FEATURE)


def ready() -> tuple[bool, str]:
    """Whether heads can be detected right now."""
    ok, reason = dependencies.deps_ok(FEATURE)
    if not ok:
        return False, reason
    if not onnx_runtime.runtime_ok():
        return False, "onnxruntime is not installed."
    if assets.missing_files(FEATURE):
        return False, "The head detector is not downloaded."
    return True, ""
