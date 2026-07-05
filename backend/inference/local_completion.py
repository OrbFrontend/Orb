"""In-process CPU text completion for input-box autocomplete.

Opt-in: needs the ``llama-cpp-python`` extra (``requirements-ml.txt``) and a
GGUF on disk. Base Orb never imports ``llama_cpp`` — the import lives inside
functions so a stock install runs fine and the autocomplete route just 503s.

The same runtime will host the planned AI-slop classifier.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Mapping, Sequence

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_MODEL = os.path.join(_ROOT, "granite-4.0-350m-base-Q8_0.gguf")

# Decoding knobs — the calibration surface for output quality. A small model
# loops ("word word word") without a firm repetition penalty; llama.cpp's 1.1
# default is too weak here. This is the main lever, tune before blaming the model.
_REPEAT_PENALTY = 1.1
_FREQUENCY_PENALTY = 0.5
_TOP_P = 0.9
_TOP_K = 40

# Llama is a single, non-reentrant context; serialize every call through it.
_lock = asyncio.Lock()
_llama: Any = None
_load_error: str | None = None


def model_path() -> str:
    return os.environ.get("ORB_AUTOCOMPLETE_MODEL") or _DEFAULT_MODEL


def _import_llama():
    from llama_cpp import Llama  # noqa: PLC0415 — deferred so base Orb needs no ML deps

    return Llama


def available() -> tuple[bool, str]:
    """Cheap check (no model load): is the extra installed and the GGUF present?"""
    try:
        _import_llama()
    except Exception as e:  # ModuleNotFoundError or a broken build
        return False, f"llama-cpp-python not installed ({e})"
    path = model_path()
    if not os.path.exists(path):
        return False, f"model file not found: {path}"
    return True, ""


def _load_blocking() -> None:
    global _llama, _load_error
    if _llama is not None or _load_error is not None:
        return
    try:
        Llama = _import_llama()
        _llama = Llama(
            model_path=model_path(),
            n_ctx=1024,
            # ponytail: capped low — this is a background helper, never the main model.
            n_threads=int(os.environ.get("ORB_AUTOCOMPLETE_THREADS", "4")),
            verbose=False,
        )
    except Exception as e:  # bad wheel, unknown arch (e.g. LFM2 unsupported), OOM
        _load_error = f"failed to load {model_path()}: {e}"


def _complete_blocking(prompt: str, n_predict: int, stop: Sequence[str], temperature: float) -> str:
    _load_blocking()
    if _llama is None:
        raise RuntimeError(_load_error or "model unavailable")
    out = _llama.create_completion(
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


async def complete(
    prompt: str,
    n_predict: int = 12,
    stop: Sequence[str] = ("\n",),
    temperature: float = 0.3,
) -> str:
    """Raw continuation of *prompt* (no chat template). Lazy-loads on first call.

    Serialized by ``_lock`` (Llama isn't reentrant) and run off the event loop so
    the blocking C call never stalls in-flight generation or the SSE keepalive.
    """
    async with _lock:
        return await asyncio.to_thread(_complete_blocking, prompt, n_predict, stop, temperature)


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
    # ponytail: naive char truncation; if quality needs it, trim on token count.
    """
    lines: list[str] = []
    summary = (char_summary or "").strip()
    if summary:
        lines.append(summary[:max_summary_chars])
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
