"""Check and install optional local-model dependencies."""

from __future__ import annotations

import os
import sys

from .catalog import MODELS

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def import_llama():
    """The ``Llama`` class, imported on demand.

    Public because it is the one deferred import two modules need: this one to
    answer ``deps_ok``, and ``local_ml`` to actually load a model with it.
    """
    from llama_cpp import Llama  # noqa: PLC0415 — deferred so base Orb needs no ML deps

    return Llama


def _shell_quote(path: str) -> str:
    """Quote only when needed — `C:\\Program Files\\...` breaks an unquoted paste."""
    return f'"{path}"' if " " in path else path


def install_cmd() -> str:
    """Install command for THIS interpreter, fully qualified — a bare `pip` targets
    whatever's on PATH, not the venv/uv env the server actually runs under, so the
    extras land in the wrong Python and the button stays gray; and a bare
    requirements filename only resolves if the shell happens to be cwd'd into the
    repo, which a fresh cmd prompt is not."""
    req = os.path.join(_ROOT, "requirements-ml.txt")
    return f"{_shell_quote(sys.executable)} -m pip install -r {_shell_quote(req)}"


def deps_ok(feature: str | None = None) -> tuple[bool, str]:
    """Cheap check (no model load): are *feature*'s extras importable?

    Per-runtime, because the features no longer share one answer. A
    ``llama_server`` feature drives a child process over HTTP and needs only
    ``huggingface_hub``, to fetch the weights; ``llama_cpp`` features run the
    model in-process and need the binding too. ``feature=None`` keeps the
    original whole-extras meaning, which is what the Local ML card's top-level
    ``deps_ok`` (the grouped opt-in) is keyed on; every per-feature caller
    passes a name.
    """
    runtime = MODELS[feature].runtime if feature in MODELS else "llama_cpp"
    try:
        if runtime == "llama_cpp":
            import_llama()
        import huggingface_hub  # noqa: F401, PLC0415 — deferred; only needed for downloads
    except Exception as e:  # ModuleNotFoundError or a broken build
        return False, f"ML extras not installed ({e}); {install_cmd()}"
    return True, ""
