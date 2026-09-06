"""In-process local-ML inference through llama-cpp-python."""

from __future__ import annotations

import asyncio
import atexit
import math
import os
from collections.abc import Sequence
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
