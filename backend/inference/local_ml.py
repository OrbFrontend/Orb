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
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.text_segmentation import remove_quoted_spans, split_sentences
from .prose_rewriter.catalog import Variant

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class ModelSpec:
    """One local-ML feature's weights, and how they are run.

    ``runtime`` names the engine, because the two differ in what they need
    installed: ``llama_cpp`` features are in-process and want the full
    ``requirements-ml.txt``; ``llama_server`` features (the prose rewriter)
    drive a child ``llama-server`` over HTTP and need only ``huggingface_hub``,
    for the download. ``deps_ok(feature)`` reads this — a stock install with
    just ``huggingface_hub`` can run the rewriter and nothing else.

    ``variants`` is non-empty when the feature ships several interchangeable
    checkpoints the user picks between. The spec's own ``filename`` then names
    the default one, so every path that predates variants (``resolve_path``,
    ``download``) keeps working unchanged.
    """

    repo_id: str
    filename: str  # path *inside the HF repo* — upstream's layout, not ours
    size_mb: int
    revision: str  # pinned commit sha — a repo re-point can't swap the weights under us
    runtime: str = "llama_cpp"  # llama_cpp (in-process) | llama_server (child process)
    variants: tuple[Variant, ...] = ()

    @property
    def local_name(self) -> str:
        """On-disk name under data/models/, always flat.

        Upstream repos disagree about where a GGUF lives — root, ``gguf/``,
        ``GGUF/`` — and mirroring that gave us a tree whose two case-variant
        directories are ONE directory on macOS/Windows. Basenames must stay
        unique across MODELS *and* across every spec's variants — a name two
        specs both claim is one file two features would fight over, and a
        variant name no spec claims is a file ``prune_stale`` deletes the next
        time anything downloads. ``test_prose_rewriter_catalog`` asserts both.
        """
        return os.path.basename(self.filename)

    def all_names(self) -> set[str]:
        """Every basename this spec puts under data/models/ — the prune claim."""
        return {self.local_name, *(v.local_name for v in self.variants)}


# The two prose-rewriter repos, pinned. Named once because three variants
# share them and a half-updated pin is a silently different model.
_PROSE_1_7B_REPO = "chartreuse-verte/prose-rewriter-1.7b-v1.2"
_PROSE_1_7B_REV = "9376423c82bf236c9cfc8d79fa1675fa104d4979"
_PROSE_4B_REPO = "chartreuse-verte/prose-rewriter-4b-v1.2"
_PROSE_4B_REV = "b73aa3290a81024389e06c9d525238071ccbe480"

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
    # Not an in-process model: served by a child llama-server (see
    # inference/prose_rewriter/). `filename`/`size_mb` name the default variant
    # so the legacy single-file paths below keep working; the selector reads
    # `variants`, and every basename here must also be claimed by prune_stale.
    "prose_rewriter": ModelSpec(
        repo_id=_PROSE_4B_REPO,
        filename="GGUF/prose-rewriter-4b-v1.2-Q8_0.gguf",
        size_mb=4694,
        revision=_PROSE_4B_REV,
        runtime="llama_server",
        variants=(
            Variant(
                id="1.7b-q8",
                label="1.7B · Q8_0",
                detail="Fastest, good enough.",
                repo_id=_PROSE_1_7B_REPO,
                path="GGUF/prose-rewriter-1.7b-v1.2-Q8_0.gguf",
                revision=_PROSE_1_7B_REV,
                size_mb=2165,
                params="1.7B",
                quant="Q8_0",
            ),
            Variant(
                id="4b-q4km",
                label="4B · Q4_K_M",
                detail="Medium quality.",
                repo_id=_PROSE_4B_REPO,
                path="GGUF/prose-rewriter-4b-v1.2-Q4_K_M.gguf",
                revision=_PROSE_4B_REV,
                size_mb=2716,
                params="4B",
                quant="Q4_K_M",
            ),
            Variant(
                id="4b-q8",
                label="4B · Q8_0",
                detail="Best quality, invents the least.",
                repo_id=_PROSE_4B_REPO,
                path="GGUF/prose-rewriter-4b-v1.2-Q8_0.gguf",
                revision=_PROSE_4B_REV,
                size_mb=4694,
                params="4B",
                quant="Q8_0",
            ),
        ),
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
    for candidate in (
        os.path.join(model_dir(), spec.local_name),  # flat — what download() writes
        os.path.join(model_dir(), spec.filename),  # legacy: hf's mirror of the repo layout
        os.path.join(_ROOT, spec.local_name),  # legacy: manual drop at repo root
        os.path.join(_ROOT, spec.filename),  # legacy: mirrored drop at repo root
    ):
        if os.path.exists(candidate):
            return candidate
    return os.path.join(model_dir(), spec.local_name)  # absent: name the flat path in errors


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
            _import_llama()
        import huggingface_hub  # noqa: F401, PLC0415 — deferred; only needed for downloads
    except Exception as e:  # ModuleNotFoundError or a broken build
        return False, f"ML extras not installed ({e}); {install_cmd()}"
    return True, ""


def present(feature: str) -> bool:
    """Is *feature* usable from disk?

    For a variant-bearing spec that means ANY variant is downloaded — the
    Settings card flips from "download something" to "pick one and enable" on
    the first file, not on the default one.
    """
    spec = MODELS.get(feature)
    if spec is not None and spec.variants:
        return any(os.path.exists(os.path.join(model_dir(), v.local_name)) for v in spec.variants)
    return os.path.exists(resolve_path(feature))


def prune_stale(root: str | None = None) -> None:
    """Delete any .gguf under data/models/ that no current MODELS spec claims.

    Runs after every download so bumping a model (e.g. v2 typeahead) doesn't leave
    the old weights eating disk. Only touches .gguf files — hf's .cache bookkeeping
    and manual drops of other extensions are left alone.

    Claim is by *basename*, not full path: comparing paths meant a model sitting in
    a legacy mirrored subdir read as unclaimed and got deleted the moment any other
    feature downloaded — and on a case-insensitive filesystem, where ``GGUF/`` and
    ``gguf/`` are one directory, that fired on a model we had just fetched.
    """
    root = root or model_dir()
    # Every basename a spec puts on disk, VARIANTS INCLUDED. A variant the claim
    # set forgets is wiped the next time any feature downloads — 4.7 GB gone
    # because an unrelated button was pressed.
    keep = {name for s in MODELS.values() for name in s.all_names()}
    walked: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".cache"]  # hf's bookkeeping is its own business
        for name in files:
            if name.endswith(".gguf") and name not in keep:
                os.remove(os.path.join(dirpath, name))
        if dirpath != root:
            walked.append(dirpath)
    for d in reversed(walked):  # deepest first, so the emptied-out gguf//GGUF/ mirrors collapse
        if not os.listdir(d):
            os.rmdir(d)


def variant_spec(feature: str, variant_id: str | None) -> tuple[str, str, str, str]:
    """``(repo_id, path_in_repo, revision, local_name)`` for one download.

    A ``variant_id`` names one of the spec's variants; ``None`` falls back to
    the spec's own file, which is what every single-file feature uses.
    """
    spec = MODELS[feature]
    if variant_id:
        for v in spec.variants:
            if v.id == variant_id:
                return v.repo_id, v.path, v.revision, v.local_name
        raise ValueError(f"Unknown variant {variant_id!r} for {feature!r}")
    return spec.repo_id, spec.filename, spec.revision, spec.local_name


def download(feature: str, variant: str | None = None) -> None:
    """Fetch a GGUF into data/models/, then prune stale weights. Blocking; run in a thread."""
    from huggingface_hub import hf_hub_download  # noqa: PLC0415 — deferred

    repo_id, path, revision, local_name = variant_spec(feature, variant)
    got = hf_hub_download(repo_id=repo_id, filename=path, revision=revision, local_dir=model_dir())
    flat = os.path.join(model_dir(), local_name)
    if os.path.normpath(got) != os.path.normpath(flat):
        os.replace(got, flat)  # hf mirrors the repo's own gguf//GGUF/ nesting; we don't keep it
    prune_stale()  # after fetch: new file lands before old ones go, so a failed download keeps the old model


def delete_model(feature: str, variant: str | None = None) -> bool:
    """Remove one downloaded GGUF. Returns whether a file was actually deleted.

    Exists because the three rewriter variants are 9.6 GB combined and "find
    the folder yourself" is not an acceptable only exit at that size.
    """
    _repo, _path, _rev, local_name = variant_spec(feature, variant)
    target = os.path.join(model_dir(), local_name)
    if not os.path.exists(target):
        return False
    os.remove(target)
    return True


def available(feature: str = "autocomplete") -> tuple[bool, str]:
    """Feature readiness: extras installed AND this feature's model present."""
    ok, reason = deps_ok(feature)
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

    # Self-check for the destructive prune (temp dir; never touches real models).
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        keep = os.path.join(d, MODELS["autocomplete"].local_name)
        open(keep, "w").close()
        mirrored = os.path.join(d, MODELS["pov_classifier"].filename)  # legacy gguf/ nesting
        os.makedirs(os.path.dirname(mirrored), exist_ok=True)
        open(mirrored, "w").close()
        stale = os.path.join(d, "old-granite-Q8_0.gguf")
        open(stale, "w").close()
        notes = os.path.join(d, "readme.txt")  # non-gguf must survive
        open(notes, "w").close()
        cached = os.path.join(d, ".cache", "huggingface", "download")
        os.makedirs(cached)
        prune_stale(d)
        assert os.path.exists(keep), "current spec's gguf must be kept"
        assert os.path.exists(mirrored), "a claimed gguf in a legacy subdir must survive"
        assert not os.path.exists(stale), "unclaimed gguf must be removed"
        assert os.path.exists(notes), "non-gguf must be left alone"
        assert os.path.isdir(cached), "hf's .cache must be left alone, empty or not"
    print("prune_stale OK")
