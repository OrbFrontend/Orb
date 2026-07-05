"""Shared in-process CPU local-ML scaffold.

Opt-in: needs the ``requirements-ml.txt`` extras (``llama-cpp-python`` +
``huggingface_hub``) and a GGUF on disk. Base Orb never imports either — the
imports live inside functions so a stock install runs fine and each local-ML
route just 503s.

One registry (``MODELS``), one download path, and per-feature cached ``Llama``
handles. Today's only feature is input-box ``autocomplete``; the planned
AI-slop classifier plugs in as another ``MODELS`` entry with no new plumbing.
The ``available`` / ``complete`` / ``build_prompt`` names are the autocomplete
route's stable surface.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class ModelSpec:
    repo_id: str
    filename: str


MODELS: dict[str, ModelSpec] = {
    "autocomplete": ModelSpec(
        repo_id="ibm-granite/granite-4.0-350m-base-GGUF",
        filename="granite-4.0-350m-base-Q8_0.gguf",
    ),
    # "slop_classifier": ModelSpec(...),   # future — repo TBD, no code yet
}

_REPEAT_PENALTY = 1.5
_FREQUENCY_PENALTY = 0.5
_TOP_P = 0.9
_TOP_K = 20

# Llama is a single, non-reentrant context; serialize every call through a
# per-feature lock so two features never share one handle's thread of execution.
_llamas: dict[str, Any] = {}
_load_errors: dict[str, str] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock(feature: str) -> asyncio.Lock:
    return _locks.setdefault(feature, asyncio.Lock())


def model_dir() -> str:
    d = os.path.join(_ROOT, "backend", "data", "models")
    os.makedirs(d, exist_ok=True)
    return d


def resolve_path(feature: str) -> str:
    """Where feature's GGUF lives: env override → data/models → repo root (back-compat)."""
    if feature == "autocomplete":
        env = os.environ.get("ORB_AUTOCOMPLETE_MODEL")
        if env:
            return env
    spec = MODELS[feature]
    in_data = os.path.join(model_dir(), spec.filename)
    if os.path.exists(in_data):
        return in_data
    return os.path.join(_ROOT, spec.filename)  # legacy: manual drop at repo root


def _import_llama():
    from llama_cpp import Llama  # noqa: PLC0415 — deferred so base Orb needs no ML deps

    return Llama


def install_cmd() -> str:
    """Install command for THIS interpreter — a bare `pip` targets whatever's on
    PATH, not the venv/uv env the server actually runs under, so the extras land
    in the wrong Python and the button stays gray."""
    return f"{sys.executable} -m pip install -r requirements-ml.txt"


def deps_ok() -> tuple[bool, str]:
    """Cheap check (no model load): are both ML extras importable?"""
    try:
        _import_llama()
        import huggingface_hub  # noqa: F401, PLC0415 — deferred; only needed for downloads
    except Exception as e:  # ModuleNotFoundError or a broken build
        return False, f"ML extras not installed ({e}); {install_cmd()}"
    return True, ""


def present(feature: str) -> bool:
    return os.path.exists(resolve_path(feature))


def download(feature: str) -> None:
    """Fetch feature's GGUF into data/models/. Blocking; run in a thread."""
    from huggingface_hub import hf_hub_download  # noqa: PLC0415 — deferred

    spec = MODELS[feature]
    # ponytail: synchronous download in a threadpool, no progress bar — ~370 MB;
    # add SSE progress only if users complain.
    hf_hub_download(repo_id=spec.repo_id, filename=spec.filename, local_dir=model_dir())


def available() -> tuple[bool, str]:
    """Autocomplete-facing readiness: extras installed AND its model present."""
    ok, reason = deps_ok()
    if not ok:
        return False, reason
    if not present("autocomplete"):
        return False, f"model file not found: {resolve_path('autocomplete')}"
    return True, ""


def _load_blocking(feature: str) -> None:
    if feature in _llamas or feature in _load_errors:
        return
    try:
        Llama = _import_llama()
        _llamas[feature] = Llama(
            model_path=resolve_path(feature),
            n_ctx=1024,
            # ponytail: capped low — these are background helpers, never the main model.
            n_threads=int(os.environ.get("ORB_AUTOCOMPLETE_THREADS", "4")),
            verbose=False,
        )
    except Exception as e:  # bad wheel, unknown arch, OOM
        _load_errors[feature] = f"failed to load {resolve_path(feature)}: {e}"


def _complete_blocking(feature: str, prompt: str, n_predict: int, stop: Sequence[str], temperature: float) -> str:
    _load_blocking(feature)
    llama = _llamas.get(feature)
    if llama is None:
        raise RuntimeError(_load_errors.get(feature) or "model unavailable")
    out = llama.create_completion(
        prompt=prompt,
        max_tokens=n_predict,
        stop=list(stop),
        temperature=temperature,
        top_p=_TOP_P,
        top_k=_TOP_K,
        repeat_penalty=_REPEAT_PENALTY,
        frequency_penalty=_FREQUENCY_PENALTY,
    )
    return out["choices"][0]["text"]


async def acomplete(
    feature: str,
    prompt: str,
    n_predict: int = 12,
    stop: Sequence[str] = ("\n",),
    temperature: float = 0.3,
) -> str:
    """Raw continuation of *prompt* using *feature*'s model (no chat template).

    Lazy-loads on first call. Serialized by the feature's lock (Llama isn't
    reentrant) and run off the event loop so the blocking C call never stalls
    in-flight generation or the SSE keepalive.
    """
    async with _lock(feature):
        return await asyncio.to_thread(_complete_blocking, feature, prompt, n_predict, stop, temperature)


async def complete(
    prompt: str,
    n_predict: int = 12,
    stop: Sequence[str] = ("\n",),
    temperature: float = 0.3,
) -> str:
    """Autocomplete continuation — thin alias over ``acomplete('autocomplete', ...)``."""
    return await acomplete("autocomplete", prompt, n_predict, stop, temperature)


def build_prompt(
    char_name: str,
    user_name: str,
    char_summary: str,
    recent: Sequence[Mapping[str, str]],
    draft: str,
    *,
    max_msg_chars: int = 500,
    max_summary_chars: int = 400,
) -> str:
    """Assemble a short raw-continuation prompt ending at the user's draft.

    *recent* is oldest→newest ``{"role": "user"|"assistant", "content": str}``.
    Deliberately excludes the Director/pipeline injection block — this is a
    lightweight typeahead, not a full turn. The model continues the final line.
    """
    lines: list[str] = []
    summary = (char_summary or "").strip()
    if summary:
        lines.append(summary[:max_summary_chars])
        lines.append("***Roleplay chat below***")
    for m in recent:
        name = user_name if m.get("role") == "user" else char_name
        content = (m.get("content") or "").strip()[:max_msg_chars]
        if content:
            lines.append(f"{name}: {content}")
    # No trailing newline: the model continues this exact line.
    lines.append(f"{user_name}: {draft}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Self-check for the pure trimmer (no model needed).
    p = build_prompt(
        "Aria",
        "Sam",
        "Aria is a wry tavern keeper.",
        [{"role": "assistant", "content": "You look lost."}, {"role": "user", "content": "Maybe I am."}],
        "I walk into the",
    )
    assert p.endswith("Sam: I walk into the"), p
    assert "Aria: You look lost." in p
    assert "Aria is a wry tavern keeper." in p
    assert "Director" not in p and "Scene Direction" not in p
    print("build_prompt OK\n---\n" + p)
