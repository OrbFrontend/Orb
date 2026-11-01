"""Check and install optional local-model dependencies."""

from __future__ import annotations

import os
import sys

from .catalog import MODELS, RuntimeKind

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def import_llama():
    """The ``Llama`` class, imported on demand.

    Public because it is the one deferred import two modules need: this one to
    answer ``deps_ok``, and ``local_ml`` to actually load a model with it.
    """
    from llama_cpp import Llama  # noqa: PLC0415 -- deferred so base Orb needs no ML deps

    return Llama


def _shell_quote(path: str) -> str:
    """Quote only when needed -- `C:\\Program Files\\...` breaks an unquoted paste."""
    return f'"{path}"' if " " in path else path


#: The requirements file that installs each runtime's extras. llama_server needs only base requirements.
_REQUIREMENTS: dict[RuntimeKind, str] = {
    "llama_cpp": "requirements-ml.txt",
    "llama_server": "requirements.txt",
    "onnx": "requirements-onnx.txt",
}


def _runtime(feature: str | None) -> RuntimeKind:
    return MODELS[feature].runtime if feature in MODELS else "llama_cpp"


def install_cmd(feature: str | None = None) -> str:
    """Install command for THIS interpreter, fully qualified -- a bare `pip` targets whatever's on PATH, not the venv/uv env the
    server actually runs under, so the extras land in the wrong Python and the button stays gray; and a bare requirements
    filename only resolves if the shell happens to be cwd'd into the repo, which a fresh cmd prompt is not.
    """
    req = os.path.join(_ROOT, _REQUIREMENTS[_runtime(feature)])
    return f"{_shell_quote(sys.executable)} -m pip install -r {_shell_quote(req)}"


def deps_ok(feature: str | None = None) -> tuple[bool, str]:
    """Check importable extras without loading models.

    llama_server needs download support; llama_cpp also needs its binding; ONNX runtime needs onnxruntime, not the
    graph-building onnx package. feature=None checks the llama_cpp extras for the grouped opt-in card.
    """
    runtime = _runtime(feature)
    try:
        if runtime == "llama_cpp":
            import_llama()
        if runtime == "onnx":
            import onnxruntime  # noqa: F401, PLC0415 -- deferred; base Orb has no ML extras
        import huggingface_hub  # noqa: F401, PLC0415 -- deferred; only needed for downloads
    except Exception as e:  # ModuleNotFoundError or a broken build
        return False, f"ML extras not installed ({e}); {install_cmd(feature)}"
    return True, ""
