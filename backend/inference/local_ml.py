"""In-process local-ML inference: the ``llama-cpp-python`` half.

THE OTHER HALF IS :mod:`backend.inference.local_models`, one letter away and
genuinely different: that package owns model *artifacts* (the manifest, the
download, the prune) and the supervised ``llama-server`` child. This module
owns the calls Orb makes into a GGUF loaded inside this process, and it
re-exports the artifact surface because ``workflows/toolkit.py`` hands this
module to workflow authors under this name.

Opt-in: needs the ``requirements-ml.txt`` extras (``llama-cpp-python`` +
``huggingface_hub``) and a GGUF on disk. Base Orb never imports either — the
imports live inside functions so a stock install runs fine and each local-ML
route just 503s.

``autocomplete`` is a text-generation model (``create_completion``); the rest
are ModernBERT sequence classifiers read through ``_head_logits`` and differing
only in how they interpret those logits — ``ascore`` softmaxes two,
``aclassify`` argmaxes 28, ``aclassify_pov`` marginalizes a 4x3 grid. A new
model reuses the download/toggle/path plumbing for free, but its *inference*
path is its own — generation and classification don't share a call. The
``available`` / ``complete`` / ``build_prompt`` / ``ascore`` names are the
routes' stable surface.
"""

from __future__ import annotations

import asyncio
import atexit
import math
import os
from collections.abc import Mapping, Sequence
from typing import Any

from ..core.text_segmentation import remove_quoted_spans, split_sentences
from .local_models import (
    MODELS,
    ModelSpec,
    ModelVariantSpec,
    available,
    delete_model,
    deps_ok,
    download,
    import_llama,
    install_cmd,
    model_dir,
    present,
    prune_stale,
    resolve_path,
    variant_path,
    variant_present,
    variant_spec,
)

#: Re-exported from :mod:`local_models` for callers that address this module by
#: name — ``workflows/toolkit.py`` publishes it as the workflow author's API,
#: and the Local ML routes and tests import from here. NOTE FOR TESTS: these
#: are second bindings. Production code calls ``local_models``' own copies, so
#: a monkeypatch belongs on the module that OWNS the name (``assets.download``,
#: ``dependencies.deps_ok``), not on the re-export.
__all__ = [
    "GO_EMOTIONS",
    "MODELS",
    "POV_ROWS",
    "ModelSpec",
    "ModelVariantSpec",
    "acomplete",
    "aclassify",
    "aclassify_pov",
    "ascore",
    "available",
    "build_prompt",
    "complete",
    "delete_model",
    "deps_ok",
    "download",
    "install_cmd",
    "model_dir",
    "pov_from_logits",
    "pov_input",
    "present",
    "prune_stale",
    "resolve_path",
    "variant_path",
    "variant_present",
    "variant_spec",
]


# The 28 go-emotions labels in standard id2label order (neutral last, index 27).
# Order MUST match the GGUF head's logit order — the classifier reads argmax(v[0:28])
# and maps back through this tuple. Also the standard expression-pack label set.
GO_EMOTIONS: tuple[str, ...] = (
    "admiration",
    "amusement",
    "anger",
    "annoyance",
    "approval",
    "caring",
    "confusion",
    "curiosity",
    "desire",
    "disappointment",
    "disapproval",
    "disgust",
    "embarrassment",
    "excitement",
    "fear",
    "gratitude",
    "grief",
    "joy",
    "love",
    "nervousness",
    "optimism",
    "pride",
    "realization",
    "relief",
    "remorse",
    "sadness",
    "surprise",
    "neutral",
)

# The POV half of the povtense head, whose 12 logits are a row-major 4x3 grid:
# POV rows x tense columns (past, present, ambiguous). Row-sum the softmax for the
# POV, column-sum it for the tense. Order MUST match the GGUF head's logit order.
# Only the rows are consumed -- an image prompt has no tense, so the columns are
# summed by nobody and the tense half of the model is deliberately unused.
POV_ROWS: tuple[str, ...] = ("first", "second", "third", "ambiguous")
_POV_TENSES = 3

_REPEAT_PENALTY = 1.1
_FREQUENCY_PENALTY = 0.1
_TOP_P = 0.8
_TOP_K = 5

# Llama is a single, non-reentrant context; serialize every call through a
# per-feature lock so two features never share one handle's thread of execution.
_llamas: dict[str, Any] = {}
_load_errors: dict[str, str] = {}
_locks: dict[str, asyncio.Lock] = {}


@atexit.register
def _close_llamas() -> None:
    """Free handles while llama_cpp's globals still exist.

    Left to GC, ``Llama.__del__`` runs after interpreter shutdown has nulled the
    llama_cpp module globals and dies on ``llama_model_free is None`` — noisy
    "Exception ignored in" tracebacks at the tail of every test run.
    """
    while _llamas:
        _, llama = _llamas.popitem()
        try:
            llama.close()
        except Exception:  # nothing left to salvage at exit
            pass


def _lock(feature: str) -> asyncio.Lock:
    return _locks.setdefault(feature, asyncio.Lock())


def _load_blocking(feature: str) -> None:
    if feature in _llamas or feature in _load_errors:
        return
    try:
        Llama = import_llama()
        _llamas[feature] = Llama(
            model_path=resolve_path(feature),
            n_ctx=1024,
            n_threads=int(os.environ.get("ORB_AUTOCOMPLETE_THREADS", "4")),
            # prefill is compute-bound (gen is bandwidth-bound: more threads don't
            # help it); measured flat beyond 8 batch threads.
            n_threads_batch=int(os.environ.get("ORB_AUTOCOMPLETE_BATCH_THREADS", "8")),
            flash_attn=True,
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
    temperature: float = 0.25,
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
    temperature: float = 0.25,
) -> str:
    """Autocomplete continuation over ``acomplete('autocomplete', ...)``.

    Works around a tokenization quirk of the typeahead model: a prompt ending in
    whitespace generates garbage ("I hold up both " fails where "I hold up both"
    works). rstrip the prompt before generating; build_prompt guarantees the only
    trailing whitespace is the user's draft tail, so this trims exactly the draft.
    When we did trim, the user already typed the word separator, so lstrip the
    model's re-emitted leading space back off — the frontend appends the completion
    to the untrimmed draft, and "...both " + " hands" would double the space.
    """
    trimmed = prompt.rstrip()
    completion = await acomplete("autocomplete", trimmed, n_predict, stop, temperature)
    return completion.lstrip() if trimmed != prompt else completion


# --- Sequence classification (slop scorer) -------------------------------------
# A separate Llama mode from generation: the GGUF carries a 2-class head, scored
# with RANK pooling. `embed()` then returns a buffer whose first two floats are
# the class logits (rest is uninitialized) — softmax them, class 1 is "slop".
_SLOP_MAX_CHARS = 2000  # ~n_ctx guard: one over-long "sentence" can't blow past 512 tokens


def _load_scorer_blocking(feature: str) -> None:
    if feature in _llamas or feature in _load_errors:
        return
    try:
        import llama_cpp  # noqa: PLC0415 — deferred; need the pooling-type constant

        _llamas[feature] = llama_cpp.Llama(
            model_path=resolve_path(feature),
            embedding=True,
            pooling_type=llama_cpp.LLAMA_POOLING_TYPE_RANK,
            n_ctx=512,
            n_threads=int(os.environ.get("ORB_AUTOCOMPLETE_THREADS", "4")),
            verbose=False,
        )
    except Exception as e:  # bad wheel, unknown arch, OOM
        _load_errors[feature] = f"failed to load {resolve_path(feature)}: {e}"


def _score_blocking(feature: str, sentences: Sequence[str]) -> list[float]:
    _load_scorer_blocking(feature)
    llama = _llamas.get(feature)
    if llama is None:
        raise RuntimeError(_load_errors.get(feature) or "model unavailable")
    out: list[float] = []
    for s in sentences:
        text = (s or "").strip()[:_SLOP_MAX_CHARS]
        if not text:
            out.append(0.0)
            continue
        v = llama.embed(text)
        a, b = float(v[0]), float(v[1])  # 2 class logits; softmax → P(slop)
        m = max(a, b)
        ea, eb = math.exp(a - m), math.exp(b - m)
        out.append(eb / (ea + eb))
    return out


async def ascore(feature: str, sentences: Sequence[str]) -> list[float]:
    """Per-sentence slop confidence in [0, 1] (class-1 softmax), aligned to input order.

    Lazy-loads on first call; serialized by the feature's lock (Llama isn't
    reentrant) and run off the event loop.
    """
    async with _lock(feature):
        return await asyncio.to_thread(_score_blocking, feature, list(sentences))


# --- Emotion classification (character expressions) ----------------------------
# Same RANK-pooling embed() path as the scorer, but a 28-class go-emotions head:
# argmax over the head's logits → GO_EMOTIONS[i]. The tail slice below is purely an n_ctx=512 guard, NOT a
# recency heuristic: the model (DistilBERT/go-emotions, trained on short comments)
# can't be trusted to weight late text, so the caller enforces recency by sending
# only the last few sentences (frontend sentenceTail); we just cap runaway input.
_CLASSIFY_MAX_CHARS = 1500


def _head_logits(feature: str, text: str, n: int) -> list[float]:
    """The first *n* class logits off feature's RANK-pooled classification head.

    The buffer past the head's own logits is uninitialized, so a short read is the
    one reliable signal that the GGUF carries a different head than the caller expects.
    """
    _load_scorer_blocking(feature)  # same embedding+RANK load as the scorer
    llama = _llamas.get(feature)
    if llama is None:
        raise RuntimeError(_load_errors.get(feature) or "model unavailable")
    v = llama.embed(text)
    if len(v) < n:
        raise RuntimeError(f"classifier returned {len(v)} logits, expected >={n} (wrong head?)")
    return [float(v[i]) for i in range(n)]


def _classify_blocking(feature: str, text: str) -> str:
    text = (text or "").strip()[-_CLASSIFY_MAX_CHARS:]  # n_ctx guard; caller owns recency
    if not text:
        return "neutral"
    logits = _head_logits(feature, text, len(GO_EMOTIONS))
    # No softmax — only the single top label is wanted, and argmax is invariant to it.
    return GO_EMOTIONS[max(range(len(logits)), key=logits.__getitem__)]


async def aclassify(feature: str, text: str) -> str:
    """Single latest message → single go-emotions label. Not batched (one message,
    one mood — YAGNI). Lazy-loads; serialized by the feature's lock; off the loop."""
    async with _lock(feature):
        return await asyncio.to_thread(_classify_blocking, feature, text)


# --- POV classification (image generation camera) ------------------------------
# The model card asks for "roughly 1-4 sentences without prior context", not a
# whole reply: the encoder's trained context is 256 tokens, and a raw tail slice of
# that size is 5-10 sentences that usually starts mid-word. So `pov_input` shapes
# the span instead of just capping it. Tail-anchored like the emotion path: the
# composer freezes the FINAL visible instant of a reply, so the end of the message
# is the part whose POV matters. _POV_MAX_CHARS survives only as a runaway guard
# for text with no sentence breaks at all.
_POV_MAX_CHARS = 800
_POV_SENTENCES = 3


def pov_input(text: str) -> str:
    """The span of *text* the povtense model should see: the last few narration
    sentences, dialogue removed.

    Pure, so the shaping — which decides what the model is even asked about — is
    testable without loading it. Returns "" for a reply that is all dialogue; the
    caller reads that as "ambiguous" and walks back to the previous message, which
    is the right answer for a turn that shows no narration.
    """
    narration = remove_quoted_spans(text or "")
    sentences = split_sentences(narration)
    return " ".join(sentences[-_POV_SENTENCES:]).strip()[-_POV_MAX_CHARS:]


def pov_from_logits(logits: Sequence[float]) -> str:
    """Marginalize the 4x3 povtense grid down to one POV row label.

    Pure, so the row-major layout — the one thing here that is silently wrong if
    transposed — is testable without the model.
    """
    m = max(logits)
    exp = [math.exp(x - m) for x in logits]
    # Row sums over the softmax marginalize the tense out of each POV. The
    # normalizer is constant across rows, so argmax needs no division.
    rows = [sum(exp[i * _POV_TENSES : (i + 1) * _POV_TENSES]) for i in range(len(POV_ROWS))]
    return POV_ROWS[max(range(len(rows)), key=rows.__getitem__)]


def _classify_pov_blocking(feature: str, text: str) -> str:
    shaped = pov_input(text)
    if not shaped:
        return "ambiguous"
    return pov_from_logits(_head_logits(feature, shaped, len(POV_ROWS) * _POV_TENSES))


async def aclassify_pov(text: str) -> str:
    """One message → one of POV_ROWS ("first" | "second" | "third" | "ambiguous").

    Only the span `pov_input` selects is read, not the whole message.

    "ambiguous" is a real class the model was trained to emit, not a confidence
    floor we impose, so the caller treats it as "ask the previous message" rather
    than as a failure. Lazy-loads; serialized by the feature's lock; off the loop.
    """
    async with _lock("pov_classifier"):
        return await asyncio.to_thread(_classify_pov_blocking, "pov_classifier", text)


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

    *recent* is oldest→newest ``{"role": "user"|"assistant", "content": str}``,
    each entry optionally carrying a ``"name"`` that labels that line instead of
    *char_name* — how a group scene names the member who actually spoke, since
    there every reply would otherwise be attributed to the scene itself.
    Deliberately excludes the Director/pipeline injection block — this is a
    lightweight typeahead, not a full turn. The model continues the final line.
    """
    lines: list[str] = []
    summary = (char_summary or "").strip()
    if summary:
        lines.append(summary[:max_summary_chars])
        lines.append("***Roleplay chat below***")
    for m in recent:
        name = (m.get("name") or "").strip() or (user_name if m.get("role") == "user" else char_name)
        content = (m.get("content") or "").strip()[
            -max_msg_chars:
        ]  # keep the tail: typeahead reacts to the latest action, which is at the END of the message
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
    print("build_prompt OK")

    # Self-check for the povtense grid layout (no model needed). One hot cell per
    # case, placed row-major: index = row * 3 + tense column.
    for row, label in enumerate(POV_ROWS):
        for col in range(_POV_TENSES):
            grid = [0.0] * (len(POV_ROWS) * _POV_TENSES)
            grid[row * _POV_TENSES + col] = 9.0
            assert pov_from_logits(grid) == label, (row, col, label)
    # A POV spread across all three tenses still beats a single taller cell in another row.
    spread = [0.0] * 12
    spread[6] = spread[7] = spread[8] = 2.0  # "third", split across tenses
    spread[0] = 3.0  # "first", one tense only
    assert pov_from_logits(spread) == "third", spread
    print("pov_from_logits OK")

    # Self-check for what the POV model is actually shown (no model needed).
    reply = 'She turned. "I will go," she said. He waited by the door. Rain hit the glass.'
    shaped = pov_input(reply)
    assert "I will go" not in shaped, shaped  # dialogue is not narration
    assert shaped.endswith("Rain hit the glass."), shaped  # tail-anchored
    assert len(split_sentences(shaped)) <= _POV_SENTENCES, shaped
    assert pov_input('"All of it." "Every word."') == ""  # all dialogue -> caller walks back
    assert pov_input("") == "" and pov_input("   ") == ""
    assert pov_input("no terminal punctuation here") == "no terminal punctuation here"
    print("pov_input OK")
