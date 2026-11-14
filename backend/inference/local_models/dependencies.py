"""Check and install optional local-model dependencies."""

from __future__ import annotations

import os
import sys

from .catalog import MODELS, RuntimeKind

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _shell_quote(path: str) -> str:
    """Quote only when needed -- `C:\\Program Files\\...` breaks an unquoted paste."""
    return f'"{path}"' if " " in path else path


#: The requirements file that installs each runtime's extras. llama_server needs only base requirements.
_REQUIREMENTS: dict[RuntimeKind, str] = {
    "llama_server": "requirements.txt",
    "onnx": "requirements-onnx.txt",
    "zonos_server": "requirements.txt",
}


def _runtime(feature: str | None) -> RuntimeKind:
    return MODELS[feature].runtime if feature in MODELS else "llama_server"


def install_cmd(feature: str | None = None) -> str:
    """Install command for THIS interpreter, fully qualified -- a bare `pip` targets whatever's on PATH, not the venv/uv env the
    server actually runs under, so the extras land in the wrong Python and the button stays gray; and a bare requirements
    filename only resolves if the shell happens to be cwd'd into the repo, which a fresh cmd prompt is not.
    """
    req = os.path.join(_ROOT, _REQUIREMENTS[_runtime(feature)])
    return f"{_shell_quote(sys.executable)} -m pip install -r {_shell_quote(req)}"


def deps_ok(feature: str | None = None) -> tuple[bool, str]:
    """Check importable extras without loading models.

    llama_server needs download support; ONNX runtime also needs onnxruntime, not the graph-building onnx package.
    feature=None checks the llama_server extras for the grouped opt-in card.
    """
    try:
        if _runtime(feature) == "onnx":
            import onnxruntime  # noqa: F401, PLC0415 -- deferred; base Orb has no ML extras
        import huggingface_hub  # noqa: F401, PLC0415 -- deferred; only needed for downloads
    except Exception as e:  # ModuleNotFoundError or a broken build
        return False, f"ML extras not installed ({e}); {install_cmd(feature)}"
    return True, ""
