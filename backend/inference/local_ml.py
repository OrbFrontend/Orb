"""Shared in-process CPU local-ML scaffold.

Opt-in: needs the ``requirements-ml.txt`` extras (``llama-cpp-python`` +
``huggingface_hub``) and a GGUF on disk. Base Orb never imports either — the
imports live inside functions so a stock install runs fine and each local-ML
route just 503s.

One registry (``MODELS``), one download path, and per-feature cached ``Llama``
handles. ``autocomplete`` is a text-generation model (``create_completion``);
the rest are ModernBERT sequence classifiers read through ``_head_logits`` and
differing only in how they interpret those logits — ``ascore`` softmaxes two,
``aclassify`` argmaxes 28, ``aclassify_pov`` marginalizes a 4x3 grid. A new model
reuses the download/toggle/path plumbing for free, but its *inference* path is its
own — generation and classification don't share a call. The ``available`` /
``complete`` / ``build_prompt`` / ``ascore`` names are the routes' stable surface.
"""

from __future__ import annotations

import asyncio
import atexit
import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class ModelSpec:
    repo_id: str
    filename: str
    size_mb: int
    revision: str  # pinned commit sha — a repo re-point can't swap the weights under us


MODELS: dict[str, ModelSpec] = {
    "autocomplete": ModelSpec(
        repo_id="chartreuse-verte/orb-human-typeahead-1b-v2.2",
        filename="GGUF/orb-human-typeahead-1b-v2.2-Q4_0.gguf",
        size_mb=930,
        revision="2e340db799eca2ef36ef80fc6938e40ab1ece111",
    ),
    "slop_classifier": ModelSpec(
        repo_id="chartreuse-verte/ettin150m-purple-GGUF",
        filename="ettin150m-purple-q8_0.gguf",
        size_mb=161,
        revision="125cf38d62e78b7091c23e6d523d805c7ec2f47e",
    ),
    "emotion_classifier": ModelSpec(
        repo_id="chartreuse-verte/ettin-emotion-28-multilabel-68m",
        filename="gguf/ettin-emotion-28ml-68m-q8_0.gguf",
        size_mb=71,
        revision="9f8d0100e45c133e713283499e55105f61d29118",
    ),
    "pov_classifier": ModelSpec(
        repo_id="chartreuse-verte/ettin-povtense-17m",
        filename="gguf/povtense-17m-q8_0.gguf",
        size_mb=20,
        revision="1245e55c47f9afc3d4938ef70f5228580228d899",
    ),
}

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


def model_dir() -> str:
    d = os.path.join(_ROOT, "backend", "data", "models")
    os.makedirs(d, exist_ok=True)
    return d


def resolve_path(feature: str) -> str:
    """Where feature's GGUF lives: env override → data/models → repo root (back-compat)."""
    if feature == "autocomplete":
        env = os.environ.get("ORB_AUTOCOMPLETE_MODEL")
        if env and os.path.exists(env):  # stale override must not hide a downloaded model
            return env
    spec = MODELS[feature]
    in_data = os.path.join(model_dir(), spec.filename)
    if os.path.exists(in_data):
        return in_data
    return os.path.join(_ROOT, spec.filename)  # legacy: manual drop at repo root


def _import_llama():
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


def prune_stale(root: str | None = None) -> None:
    """Delete any .gguf under data/models/ that no current MODELS spec claims.

    Runs after every download so bumping a model (e.g. v2 typeahead) doesn't leave
    the old weights eating disk. Only touches .gguf files — hf's .cache bookkeeping
    and manual drops of other extensions are left alone.
    """
    root = root or model_dir()
    keep = {os.path.normpath(os.path.join(root, s.filename)) for s in MODELS.values()}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".gguf"):
                p = os.path.normpath(os.path.join(dirpath, name))
                if p not in keep:
                    os.remove(p)


def download(feature: str) -> None:
    """Fetch feature's GGUF into data/models/, then prune stale weights. Blocking; run in a thread."""
    from huggingface_hub import hf_hub_download  # noqa: PLC0415 — deferred

    spec = MODELS[feature]
    hf_hub_download(repo_id=spec.repo_id, filename=spec.filename, revision=spec.revision, local_dir=model_dir())
    prune_stale()  # after fetch: new file lands before old ones go, so a failed download keeps the old model


def available(feature: str = "autocomplete") -> tuple[bool, str]:
    """Feature readiness: extras installed AND this feature's model present."""
    ok, reason = deps_ok()
    if not ok:
        return False, reason
    if not present(feature):
        return False, f"model file not found: {resolve_path(feature)}"
    return True, ""


def _load_blocking(feature: str) -> None:
    if feature in _llamas or feature in _load_errors:
        return
    try:
        Llama = _import_llama()
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
# Dialogue is dropped before the narration is read. An RP reply usually ends in
# speech, and speech is first-person by nature ("I'll go," she said) -- feeding it
# to a POV classifier is the single most likely way to read a third-person scene as
# first. Straight and curly quotes both; asterisk-wrapped action is narration and
# stays.
_POV_DIALOGUE_RE = re.compile(r"[\"“”«»][^\"“”«»]*[\"“”«»]")
_POV_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def pov_input(text: str) -> str:
    """The span of *text* the povtense model should see: the last few narration
    sentences, dialogue removed.

    Pure, so the shaping — which decides what the model is even asked about — is
    testable without loading it. Returns "" for a reply that is all dialogue; the
    caller reads that as "ambiguous" and walks back to the previous message, which
    is the right answer for a turn that shows no narration.
    """
    narration = _POV_DIALOGUE_RE.sub(" ", text or "")
    sentences = [s for s in _POV_SENTENCE_SPLIT_RE.split(narration.strip()) if s.strip()]
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
    assert len(_POV_SENTENCE_SPLIT_RE.split(shaped)) <= _POV_SENTENCES, shaped
    assert pov_input('"All of it." "Every word."') == ""  # all dialogue -> caller walks back
    assert pov_input("") == "" and pov_input("   ") == ""
    assert pov_input("no terminal punctuation here") == "no terminal punctuation here"
    print("pov_input OK")

    # Self-check for the destructive prune (temp dir; never touches real models).
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        keep = os.path.join(d, MODELS["autocomplete"].filename)
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        open(keep, "w").close()
        stale = os.path.join(d, "old-granite-Q8_0.gguf")
        open(stale, "w").close()
        notes = os.path.join(d, "readme.txt")  # non-gguf must survive
        open(notes, "w").close()
        prune_stale(d)
        assert os.path.exists(keep), "current spec's gguf must be kept"
        assert not os.path.exists(stale), "unclaimed gguf must be removed"
        assert os.path.exists(notes), "non-gguf must be left alone"
    print("prune_stale OK")
