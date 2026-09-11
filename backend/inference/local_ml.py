"""In-process local-ML inference through llama-cpp-python."""

from __future__ import annotations

import asyncio
import atexit
import math
import os
import re
from collections.abc import Sequence
from typing import Any

from ..core.text_segmentation import (
    HARD_LINE_BREAK_RE,
    remove_quoted_spans,
    split_sentences,
    strip_protected_markup,
)
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
#: name. The workflow toolkit wraps the small capabilities plug-ins need; Local
#: ML routes and tests import this implementation module directly. NOTE FOR TESTS: these
#: are second bindings. Production code calls ``local_models``' own copies, so
#: a monkeypatch belongs on the module that OWNS the name (``assets.download``,
#: ``dependencies.deps_ok``), not on the re-export.
__all__ = [
    "DIALOGUE_COLS",
    "GO_EMOTIONS",
    "MARKUP_INPUT_CHARS",
    "MARKUP_INPUT_VERSION",
    "MODELS",
    "NARRATION_ROWS",
    "POV_ROWS",
    "TENSE_COLS",
    "ModelSpec",
    "ModelVariantSpec",
    "acomplete",
    "aclassify",
    "aclassify_markup",
    "aclassify_pov",
    "aclassify_pov_tense",
    "ascore",
    "available",
    "delete_model",
    "deps_ok",
    "download",
    "install_cmd",
    "markup_from_logits",
    "markup_input",
    "model_dir",
    "pov_from_logits",
    "pov_input",
    "present",
    "prune_stale",
    "resolve_path",
    "tense_from_logits",
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

# The povtense head's 12 logits are a row-major 4x3 grid: POV rows x tense
# columns. Row-sum the softmax for the POV, column-sum it for the tense. Order
# MUST match the GGUF head's logit order -- a transposed reading still returns a
# plausible label, so every cell is pinned by test: the rows in
# tests/unit/workflows/image_gen/test_pov.py, the columns in tests/unit/test_local_ml.py.
POV_ROWS: tuple[str, ...] = ("first", "second", "third", "ambiguous")
TENSE_COLS: tuple[str, ...] = ("past", "present", "ambiguous")
_TENSE_COUNT = len(TENSE_COLS)

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


# The v2 model still needs short windows. Keep narration extraction in callers:
# empirical dialogue-insertion probes favor it over trusting native markers
# (docs/experiments/povtense-v2.md). The model card asks for 1-4 sentences, not a
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


def _argmax(values: Sequence[float]) -> int:
    return max(range(len(values)), key=values.__getitem__)


def _grid_margins(logits: Sequence[float], rows: Sequence[str], cols: Sequence[str]) -> tuple[str, str]:
    """Read a row-major joint head as (row label, column label).

    Each label is the argmax of its own softmax marginal (row sums, column sums),
    never the top cell's coordinates. The softmax stays unnormalized: dividing
    every mass by one constant cannot move an argmax.
    """
    m = max(logits)
    exp = [math.exp(x - m) for x in logits]
    width = len(cols)
    row_mass = [sum(exp[r * width : (r + 1) * width]) for r in range(len(rows))]
    col_mass = [sum(exp[r * width + c] for r in range(len(rows))) for c in range(width)]
    return rows[_argmax(row_mass)], cols[_argmax(col_mass)]


def _pov_tense_from_logits(logits: Sequence[float]) -> tuple[str, str]:
    return _grid_margins(logits, POV_ROWS, TENSE_COLS)


def pov_from_logits(logits: Sequence[float]) -> str:
    """Marginalize the 4x3 joint head to one POV row label."""
    return _pov_tense_from_logits(logits)[0]


def tense_from_logits(logits: Sequence[float]) -> str:
    """Marginalize the 4x3 joint head to one tense column label."""
    return _pov_tense_from_logits(logits)[1]


def _classify_pov_tense_blocking(feature: str, text: str) -> tuple[str, str]:
    """Both grid margins off ONE embed -- the head is a single forward pass, so
    asking for the tense separately would pay for the model twice."""
    shaped = pov_input(text)
    if not shaped:
        return "ambiguous", "ambiguous"
    logits = _head_logits(feature, shaped, len(POV_ROWS) * _TENSE_COUNT)
    return _pov_tense_from_logits(logits)


def _classify_pov_blocking(feature: str, text: str) -> str:
    return _classify_pov_tense_blocking(feature, text)[0]


async def aclassify_pov(text: str) -> str:
    """One message → one of POV_ROWS ("first" | "second" | "third" | "ambiguous").

    Only the span `pov_input` selects is read, not the whole message.

    "ambiguous" is a real class the model was trained to emit, not a confidence
    floor we impose, so the caller treats it as "ask the previous message" rather
    than as a failure. Lazy-loads; serialized by the feature's lock; off the loop.
    """
    async with _lock("pov_classifier"):
        return await asyncio.to_thread(_classify_pov_blocking, "pov_classifier", text)


async def aclassify_pov_tense(text: str) -> tuple[str, str]:
    """One message -> (POV_ROWS label, TENSE_COLS label). One model call.

    The combined entry point for callers that want both margins of the grid;
    `aclassify_pov` stays the single-label door image_gen's camera reads through.
    Lazy-loads; serialized by the feature's lock; off the loop.
    """
    async with _lock("pov_classifier"):
        return await asyncio.to_thread(_classify_pov_tense_blocking, "pov_classifier", text)


# The markup classifier (narration x dialogue convention) reads a WHOLE message:
# convention is a coverage ratio and the dead band is a whole-message property, so
# unlike `pov_input` nothing is windowed. Protected formatting runs (fenced code,
# **bold**, dividers) are removed with the exact pattern `classify_axes` ignores.
# Quote and asterisk glyphs are deliberately NOT normalized: the tokenizer reads
# every variant, and a broken or unusual mark is the very signal being classified.
# The one exception is a markdown bullet (`* item` at a line start): the parser
# already refuses to read it as emphasis, and its star becomes "-" so a list never
# looks like asterisk narration to the model. A line that closes an asterisk later
# (`* She waves. *`) is a sloppy beat rather than a list, and keeps its star.
#
# The model's own cut is the token one. `Llama.embed(truncate=True)` keeps the
# first n_batch (512) ids, so a long message loses its tail and its [SEP]; the
# trainer (../RP-Markup-Classifier) truncates the same way rather than HF's
# keep-[SEP] way, so both sides see identical ids. MARKUP_INPUT_CHARS is only a
# runaway guard: on app.db it never binds before the token cut. Training builds
# record MARKUP_INPUT_VERSION and a digest of this function's output, so bump the
# version whenever the shaping changes.
MARKUP_INPUT_VERSION = "markup-input-v2"
MARKUP_INPUT_CHARS = 4000

_LINE_BREAKS = re.compile(f"({HARD_LINE_BREAK_RE.pattern})")
_BULLET_STAR = re.compile(r"([ \t]*)\*(?=[ \t])")


def _dash_bullets(text: str) -> str:
    """Rewrite each line-start bullet star to "-"; nothing else moves."""
    parts = _LINE_BREAKS.split(text)
    for i in range(0, len(parts), 2):  # even slots are lines, odd slots their breaks
        m = _BULLET_STAR.match(parts[i])
        if m and "*" not in parts[i][m.end() :]:
            parts[i] = f"{m.group(1)}-{parts[i][m.end() :]}"
    return "".join(parts)


def markup_input(text: str) -> str:
    """The text the markup classifier sees: protected runs removed, bullet stars
    dashed, then capped.

    Pure, so the shaping that training and serving share is testable without
    loading the model.
    """
    return _dash_bullets(strip_protected_markup(text or ""))[:MARKUP_INPUT_CHARS]


# The markup head is one 9-way softmax over a row-major 3x3 grid: narration rows x
# dialogue columns (../RP-Markup-Classifier/src/schema.py). Read like povtense:
# row sums for the narration, column sums for the dialogue, never both off the top
# cell. Order MUST match the GGUF head's logit order -- a transposed read still
# returns plausible labels, so tests/unit/test_local_ml.py pins every cell.
# "unknown" is a trained class (nothing to read, or both styles mixed in this one
# message), not a confidence floor: callers read it as "leave this alone".
NARRATION_ROWS: tuple[str, ...] = ("asterisk", "bare", "unknown")
DIALOGUE_COLS: tuple[str, ...] = ("quoted", "bare", "unknown")
_MARKUP_CELLS = len(NARRATION_ROWS) * len(DIALOGUE_COLS)


def markup_from_logits(logits: Sequence[float]) -> tuple[str, str]:
    """Marginalize the 3x3 markup head to (narration, dialogue)."""
    return _grid_margins(logits, NARRATION_ROWS, DIALOGUE_COLS)


def _classify_markup_blocking(feature: str, text: str) -> tuple[str, str]:
    shaped = markup_input(text)
    if not shaped.strip():
        return "unknown", "unknown"  # nothing to read: never load the model for it
    return markup_from_logits(_head_logits(feature, shaped, _MARKUP_CELLS))


async def aclassify_markup(text: str) -> tuple[str, str]:
    """One whole message -> (NARRATION_ROWS label, DIALOGUE_COLS label). One model call.

    The model reads `markup_input(text)`, of which llama.cpp keeps the first 512
    tokens, exactly as training cut it. Lazy-loads; serialized by the feature's
    lock; off the loop.
    """
    async with _lock("markup_classifier"):
        return await asyncio.to_thread(_classify_markup_blocking, "markup_classifier", text)
