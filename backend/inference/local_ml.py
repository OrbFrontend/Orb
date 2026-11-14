"""Local-ML inference through supervised CPU llama-server children."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from ..core.text_segmentation import HARD_LINE_BREAK_RE, remove_quoted_spans, split_sentences, strip_protected_markup
from .local_models import (
    MODELS,
    ModelSpec,
    ModelVariantSpec,
    available,
    delete_model,
    deps_ok,
    download,
    file_path,
    install_cmd,
    model_dir,
    present,
    prune_stale,
    resolve_path,
    variant_path,
    variant_present,
    variant_spec,
)
from .local_models.llama_server import LaunchProfile, LlamaServerClient, ManagedLlamaServerHost

#: Re-exported from :mod:`local_models` for callers that address this module by
#: name. The workflow toolkit wraps the small capabilities plug-ins need; Local
#: ML routes and tests import this implementation module directly. NOTE FOR TESTS: these
#: are second bindings. Production code calls ``local_models``' own copies, so
#: a monkeypatch belongs on the module that OWNS the name (``assets.download``,
#: ``dependencies.deps_ok``), not on the re-export.
__all__ = [
    "GO_EMOTIONS",
    "MARKUP_INPUT_VERSION",
    "MODELS",
    "SLOP_THRESHOLD",
    "ModelSpec",
    "ModelVariantSpec",
    "SlopChunk",
    "SlopGrade",
    "acomplete",
    "aclassify",
    "aclassify_markup",
    "aclassify_pov",
    "aclassify_pov_tense_chunks",
    "aclassify_subjects",
    "acompare_subjects",
    "aemotion_scores",
    "ascore_slop",
    "available",
    "delete_model",
    "deps_ok",
    "download",
    "install_cmd",
    "model_deletion",
    "model_dir",
    "present",
    "prune_stale",
    "release",
    "resolve_path",
    "variant_path",
    "variant_present",
    "variant_spec",
]


# The 28 go-emotions labels in standard id2label order (neutral last, index 27). Order MUST match the GGUF head's logit order --
# the classifier reads argmax(v[0:28]) and maps back through this tuple. Also the standard expression-pack label set.
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

# The povtense head's 12 logits are a row-major 4x3 grid: POV rows x tense columns. Row-sum the softmax for the POV, column-sum
# it for the tense. Order MUST match the GGUF head's logit order -- a transposed reading still returns a plausible label.
_POV_ROWS: tuple[str, ...] = ("first", "second", "third", "ambiguous")
_TENSE_COLS: tuple[str, ...] = ("past", "present", "ambiguous")
_TENSE_COUNT = len(_TENSE_COLS)

_REPEAT_PENALTY = 1.1
_FREQUENCY_PENALTY = 0.1
_TOP_P = 0.8
_TOP_K = 5

# One CPU llama-server child per model, started on first use and kept resident, as the in-process handles were.
_hosts: dict[str, ManagedLlamaServerHost] = {}
_special_ids: dict[str, tuple[int, int]] = {}


def _threads() -> int:
    return int(os.environ.get("ORB_AUTOCOMPLETE_THREADS", "4"))


def _host(handle: str) -> ManagedLlamaServerHost:
    if handle not in _hosts:
        _hosts[handle] = ManagedLlamaServerHost(name=f"local_ml:{handle}", idle_timeout=math.inf)
    return _hosts[handle]


def _completion_profile(feature: str) -> LaunchProfile:
    return LaunchProfile(
        model_id=feature,
        model_path=resolve_path(feature),
        alias=feature,
        gpu_layers=0,
        ctx_size=1024,
        parallel=1,
        http_threads=4,
        threads=_threads(),
        # prefill is compute-bound (gen is bandwidth-bound: more threads don't help it); measured flat beyond 8 batch threads.
        threads_batch=int(os.environ.get("ORB_AUTOCOMPLETE_BATCH_THREADS", "8")),
        flash_attn=True,
    )


async def acomplete(
    feature: str, prompt: str, n_predict: int = 12, stop: Sequence[str] = ("\n",), temperature: float = 0.25
) -> str:
    """Raw continuation of *prompt* using *feature*'s model (no chat template). Starts the model on first call."""
    async with _host(feature).use(_completion_profile(feature)) as server:
        return await server.complete(
            prompt,
            n_predict=n_predict,
            stop=stop,
            temperature=temperature,
            top_p=_TOP_P,
            top_k=_TOP_K,
            repeat_penalty=_REPEAT_PENALTY,
            frequency_penalty=_FREQUENCY_PENALTY,
        )


# The classifier GGUFs carry a classification head, scored with RANK pooling, which leaves llama.cpp a float[n_cls_out] per
# sequence.

# A head reads the first n_ctx ids, as training cut them; a non-causal encoder needs the whole sequence in one ubatch.
_SCORER_CTX = 512
# The pair comparer is the subject analyzer's companion file, not a feature of its own; this key names its child.
_SUBJECT_COMPARER = "subjects_comparer"
_SCORER_CTX_BY_FEATURE: dict[str, int] = {"subjects_classifier": 1024, _SUBJECT_COMPARER: 2048}
# Flash attention where it was parity-checked against the HF model: 8 comparer pairs take ~40% less CPU time with it.
_SCORER_FLASH_ATTN: frozenset[str] = frozenset({_SUBJECT_COMPARER})
# Slots where one call sends several inputs: four cut a message's slop scoring by a quarter. One elsewhere, where four were
# measured no faster.
_SCORER_SLOTS: dict[str, int] = {"slop_classifier": 4, "pov_classifier": 4}
# Bounds the per-text /tokenize requests one call fans out.
_TOKENIZE_CONCURRENCY = 8


def _scorer_path(feature: str) -> str:
    if feature == _SUBJECT_COMPARER:
        return file_path(next(iter(MODELS["subjects_classifier"].extra_files)))
    return resolve_path(feature)


def _head_profile(handle: str) -> LaunchProfile:
    n_ctx = _SCORER_CTX_BY_FEATURE.get(handle, _SCORER_CTX)
    slots = _SCORER_SLOTS.get(handle, 1)
    return LaunchProfile(
        model_id=handle,
        model_path=_scorer_path(handle),
        alias=handle,
        gpu_layers=0,
        ctx_size=n_ctx * slots,
        parallel=slots,
        http_threads=4,
        batch_size=n_ctx * slots,
        ubatch_size=n_ctx,
        threads=_threads(),
        threads_batch=os.cpu_count() or 4,
        flash_attn=handle in _SCORER_FLASH_ATTN,
        rank_head=True,
    )


def _direct(text: str, n_ctx: int) -> bool:
    """Whether the child may tokenize *text* itself.

    It parses special tokens, which /tokenize is told not to; this vocabulary's control tokens are all ``[..]`` or ``<|..|>``
    literals. Every token is at least one byte, so a text this short cannot pass n_ctx with its two specials.
    """
    return len(text.encode("utf-8")) <= n_ctx - 2 and "[" not in text and "<|" not in text


_T = TypeVar("_T")


async def _bounded(calls: Sequence[Callable[[], Awaitable[_T]]]) -> list[_T]:
    gate = asyncio.Semaphore(_TOKENIZE_CONCURRENCY)

    async def run(call: Callable[[], Awaitable[_T]]) -> _T:
        async with gate:
            return await call()

    return await asyncio.gather(*(run(call) for call in calls))


async def _head_inputs(server: LlamaServerClient, texts: Sequence[str], n_ctx: int) -> list[str | list[int]]:
    inputs: list[str | list[int]] = list(texts)
    cut = [i for i, text in enumerate(texts) if not _direct(text, n_ctx)]
    ids = await _bounded([lambda text=texts[i]: server.tokenize(text, add_special=True) for i in cut])
    for i, seq in zip(cut, ids, strict=True):
        inputs[i] = seq[:n_ctx]
    return inputs


async def _logits(handle: str, texts: Sequence[str], n: int) -> list[list[float]]:
    """The first *n* head logits for each text, in input order. Starts the model on first call."""
    if not texts:
        return []
    n_ctx = _SCORER_CTX_BY_FEATURE.get(handle, _SCORER_CTX)
    async with _host(handle).use(_head_profile(handle)) as server:
        return await server.head_logits(await _head_inputs(server, texts, n_ctx), n)


# The slop head (ettin150m-purple v2) reads one paragraph per input, scored as Chartreuse's classifier/reward.py scores it
# but with a 40-word floor instead of its 60, so a flag covers fewer paragraphs: paragraphs merge with the next until a
# chunk has _SLOP_MIN_WORDS words (a short tail joins the chunk before it), a chunk past the window is read in
# sentence-aligned windows weighted by tokens, and the text's score is the word-weighted chunk mean. The threshold is
# threshold.json at the pinned revision (precision 0.9 on human-only negatives).
SLOP_THRESHOLD = 0.0933
_SLOP_MIN_WORDS = 40
_SLOP_SENTENCE_ENDS = ".!?…\n"


@dataclass(frozen=True)
class SlopChunk:
    paragraphs: tuple[int, ...]  # input indices, in order
    words: int
    score: float


@dataclass(frozen=True)
class SlopGrade:
    score: float | None  # None when there is nothing to read
    chunks: tuple[SlopChunk, ...]


def _slop_chunks(paragraphs: Sequence[str]) -> list[list[int]]:
    """Indices of the non-blank *paragraphs* in each chunk."""
    groups: list[list[int]] = []
    words: list[int] = []
    for i, paragraph in enumerate(paragraphs):
        n = len(paragraph.split())
        if not n:
            continue
        if groups and words[-1] < _SLOP_MIN_WORDS:
            groups[-1].append(i)
            words[-1] += n
        else:
            groups.append([i])
            words.append(n)
    if len(groups) > 1 and words[-1] < _SLOP_MIN_WORDS:
        last = groups.pop()
        groups[-1] += last
    return groups


def _slop_windows(n_ids: int, sentence_ends: Sequence[int], size: int) -> list[tuple[int, int]]:
    """Spans of at most *size* token positions over ``n_ids`` tokens, cut at sentence ends where possible."""
    out: list[tuple[int, int]] = []
    a = 0
    while a < n_ids:
        b = min(a + size, n_ids)
        if b < n_ids:
            cut = [e for e in sentence_ends if a < e <= b]
            b = cut[-1] if cut and cut[-1] - a >= size // 2 else b
        out.append((a, b))
        a = b
    return out


def _slop_window_ids(pieces: Sequence[tuple[int, str]], cls: int, sep: int, room: int) -> list[list[int]]:
    ids = [token for token, _ in pieces]
    ends = [i + 1 for i, (_, piece) in enumerate(pieces) if piece and piece[-1] in _SLOP_SENTENCE_ENDS]
    return [[cls, *ids[a:b], sep] for a, b in _slop_windows(len(ids), ends, room)]


def _p_class1(logits: Sequence[float]) -> float:
    """Class-1 softmax of two logits."""
    a, b = logits[0], logits[1]
    return _sigmoid(b - a)


async def _specials(server: LlamaServerClient, handle: str) -> tuple[int, int]:
    if handle not in _special_ids:
        cls, sep = await server.tokenize("", add_special=True)
        _special_ids[handle] = (cls, sep)
    return _special_ids[handle]


async def ascore_slop(paragraphs: Sequence[str]) -> SlopGrade:
    """P(slop) per chunk of *paragraphs* and for the whole text. One request for every window."""
    groups = _slop_chunks(paragraphs)
    if not groups:
        return SlopGrade(score=None, chunks=())  # nothing to read: never load the model for it
    texts = ["\n".join(paragraphs[i].strip() for i in group) for group in groups]
    handle = "slop_classifier"
    async with _host(handle).use(_head_profile(handle)) as server:
        cls, sep = await _specials(server, handle)
        pieces = await _bounded([lambda text=text: server.tokenize_pieces(text) for text in texts])
        per_chunk = [_slop_window_ids(p, cls, sep, _SCORER_CTX - 2) for p in pieces]
        logits = await server.head_logits([w for windows in per_chunk for w in windows], 2)
    chunks: list[SlopChunk] = []
    k = 0
    for group, text, windows in zip(groups, texts, per_chunk, strict=True):
        sizes = [len(w) - 2 for w in windows]
        probs = [_p_class1(v) for v in logits[k : k + len(windows)]]
        k += len(windows)
        score = sum(p * n for p, n in zip(probs, sizes, strict=True)) / max(1, sum(sizes))
        chunks.append(SlopChunk(paragraphs=tuple(group), words=len(text.split()), score=score))
    total = sum(c.words for c in chunks)
    return SlopGrade(score=sum(c.score * c.words for c in chunks) / total, chunks=tuple(chunks))


def _handles(feature: str) -> tuple[str, ...]:
    return (feature, _SUBJECT_COMPARER) if feature == "subjects_classifier" else (feature,)


async def release(feature: str) -> None:
    """Stop *feature*'s children, freeing their memory; the next call starts them again."""
    for handle in _handles(feature):
        if handle in _hosts:
            await _hosts[handle].release()


@contextlib.asynccontextmanager
async def model_deletion(feature: str) -> AsyncIterator[None]:
    """Keep *feature*'s children stopped through a model file delete, which Windows refuses while a child maps the file."""
    async with contextlib.AsyncExitStack() as stack:
        for handle in _handles(feature):
            await stack.enter_async_context(_host(handle).exclusive_release())
        yield


# Same RANK-pooled head path as the scorer, but a 28-class go-emotions head: argmax over the head's logits -> GO_EMOTIONS[i].
# The tail slice below is purely an n_ctx=512 guard, NOT a recency heuristic: the model (DistilBERT/go-emotions, trained on
# short comments) can't be trusted to weight late text, so the caller enforces recency by sending only the last few sentences
# (frontend sentenceTail); we just cap runaway input.
_CLASSIFY_MAX_CHARS = 1500


async def aclassify(feature: str, text: str) -> str:
    """Single latest message -> single go-emotions label. Not batched (one message, one mood -- YAGNI)."""
    text = (text or "").strip()[-_CLASSIFY_MAX_CHARS:]  # n_ctx guard; caller owns recency
    if not text:
        return "neutral"
    (logits,) = await _logits(feature, [text], len(GO_EMOTIONS))
    # No softmax -- only the single top label is wanted, and argmax is invariant to it.
    return GO_EMOTIONS[_argmax(logits)]


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


async def aemotion_scores(texts: Sequence[str]) -> list[dict[str, float]]:
    """Per-label go-emotions sigmoid scores for each text, in input order. One request for the whole list."""
    clipped = [(text or "").strip()[-_CLASSIFY_MAX_CHARS:] for text in texts]
    keep = [i for i, text in enumerate(clipped) if text]
    out = [dict.fromkeys(GO_EMOTIONS, 0.0) for _ in clipped]
    rows = await _logits("emotion_classifier", [clipped[i] for i in keep], len(GO_EMOTIONS))
    for i, logits in zip(keep, rows, strict=True):
        out[i] = {label: _sigmoid(value) for label, value in zip(GO_EMOTIONS, logits, strict=True)}
    return out


# The v2 model still needs short windows. Keep narration extraction in callers: empirical dialogue-insertion probes favor it
# over trusting native markers. The model card asks for 1-4 sentences, not a whole reply: the encoder's trained context is 256
# tokens, and a raw tail slice of that size is 5-10 sentences that usually starts mid-word. So `_pov_input` shapes the span
# instead of just capping it. Tail-anchored like the emotion path: the composer freezes the FINAL visible instant of a reply, so
# the end of the message is the part whose POV matters. _POV_MAX_CHARS survives only as a runaway guard for text with no
# sentence breaks at all.
_POV_MAX_CHARS = 800
_POV_SENTENCES = 3


def _pov_input(text: str) -> str:
    """The span of *text* the povtense model should see: the last few narration sentences, dialogue removed.

    Returns "" for a reply that is all dialogue; the caller reads that as "ambiguous" and walks back to the previous message, which is the right
    answer for a turn that shows no narration.
    """
    chunks = _pov_chunks(text)
    return chunks[0] if chunks else ""


def _pov_chunks(text: str) -> list[str]:
    """Every narration sentence of *text* in `_pov_input`-sized windows, newest first.

    The model reads a few sentences at a time, so a whole-message question is asked
    window by window. Windows are cut from the tail, and the first is `_pov_input`.
    """
    sentences = split_sentences(remove_quoted_spans(text or ""))
    chunks = (
        " ".join(sentences[max(0, end - _POV_SENTENCES) : end]).strip()[-_POV_MAX_CHARS:]
        for end in range(len(sentences), 0, -_POV_SENTENCES)
    )
    return [chunk for chunk in chunks if chunk]


def _argmax(values: Sequence[float]) -> int:
    return max(range(len(values)), key=values.__getitem__)


def _grid_margins(logits: Sequence[float], rows: Sequence[str], cols: Sequence[str]) -> tuple[str, str]:
    """Read a row-major joint head as (row label, column label).

    Each label is the argmax of its own softmax marginal (row sums, column sums), never the top cell's coordinates. The softmax
    stays unnormalized: dividing every mass by one constant cannot move an argmax.
    """
    m = max(logits)
    exp = [math.exp(x - m) for x in logits]
    width = len(cols)
    row_mass = [sum(exp[r * width : (r + 1) * width]) for r in range(len(rows))]
    col_mass = [sum(exp[r * width + c] for r in range(len(rows))) for c in range(width)]
    return rows[_argmax(row_mass)], cols[_argmax(col_mass)]


def _pov_tense_from_logits(logits: Sequence[float]) -> tuple[str, str]:
    return _grid_margins(logits, _POV_ROWS, _TENSE_COLS)


def _pov_from_logits(logits: Sequence[float]) -> str:
    """Marginalize the 4x3 joint head to one POV row label."""
    return _pov_tense_from_logits(logits)[0]


def _tense_from_logits(logits: Sequence[float]) -> str:
    """Marginalize the 4x3 joint head to one tense column label."""
    return _pov_tense_from_logits(logits)[1]


async def aclassify_pov(text: str) -> str:
    """One message -> one of _POV_ROWS ("first" | "second" | "third" | "ambiguous").

    Only the span `_pov_input` selects is read, not the whole message.

    "ambiguous" is a real class the model was trained to emit, not a confidence floor we impose, so the caller treats it as "ask
    the previous message" rather than as a failure.
    """
    shaped = _pov_input(text)
    if not shaped:
        return "ambiguous"
    (logits,) = await _logits("pov_classifier", [shaped], len(_POV_ROWS) * _TENSE_COUNT)
    return _pov_from_logits(logits)


async def aclassify_pov_tense_chunks(text: str) -> list[tuple[str, str]]:
    """One (POV, tense) pair per `_pov_chunks` window, newest first; ``[]`` for a reply with no narration.

    For callers that judge a whole message rather than its final instant. One request for every window; both grid margins of
    each come off its one forward pass, so asking for the tense separately would pay for the model twice.
    """
    windows = _pov_chunks(text)
    return [_pov_tense_from_logits(v) for v in await _logits("pov_classifier", windows, len(_POV_ROWS) * _TENSE_COUNT)]


# Classify markup from the whole message, excluding classify_axes protected runs. Keep quote/emphasis variants; turn genuine
# markdown bullet stars into hyphens. Ranking truncates to n_batch ids, including loss of SEP, matching training.
# _MARKUP_INPUT_CHARS is a runaway guard; bump MARKUP_INPUT_VERSION when shaping changes.
MARKUP_INPUT_VERSION = "markup-input-v2"
_MARKUP_INPUT_CHARS = 4000

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


def _markup_input(text: str) -> str:
    """The text the markup classifier sees: protected runs removed, bullet stars dashed, then capped."""
    return _dash_bullets(strip_protected_markup(text or ""))[:_MARKUP_INPUT_CHARS]


# The markup head is one 9-way softmax over a row-major 3x3 grid: narration rows x dialogue columns
# (../RP-Markup-Classifier/src/schema.py). Read like povtense: row sums for the narration, column sums for the dialogue, never
# both off the top cell. Order MUST match the GGUF head's logit order -- a transposed read still returns plausible labels.
# "unknown" is a trained class (nothing to read, or both styles mixed in this one
# message), not a confidence floor: callers read it as "leave this alone".
_NARRATION_ROWS: tuple[str, ...] = ("asterisk", "bare", "unknown")
_DIALOGUE_COLS: tuple[str, ...] = ("quoted", "bare", "unknown")
_MARKUP_CELLS = len(_NARRATION_ROWS) * len(_DIALOGUE_COLS)


def _markup_from_logits(logits: Sequence[float]) -> tuple[str, str]:
    """Marginalize the 3x3 markup head to (narration, dialogue)."""
    return _grid_margins(logits, _NARRATION_ROWS, _DIALOGUE_COLS)


async def aclassify_markup(text: str) -> tuple[str, str]:
    """One whole message -> (_NARRATION_ROWS label, _DIALOGUE_COLS label). One model call.

    The model reads `_markup_input(text)`, of which the first 512 tokens are kept, exactly as training cut it.
    """
    shaped = _markup_input(text)
    if not shaped.strip():
        return "unknown", "unknown"  # nothing to read: never load the model for it
    (logits,) = await _logits("markup_classifier", [shaped], _MARKUP_CELLS)
    return _markup_from_logits(logits)


# The subjects head is 20 categories x 3 levels, row-major: 20 separate 3-way softmaxes, categories in the trained head's
# order. A transposed read still returns plausible labels. The caller shapes
# the narration (dialogue stripped), as training did; its first 1024 ids are kept.
_SUBJECT_CATEGORIES: tuple[str, ...] = (
    "eyes",
    "hair",
    "face",
    "mouth",
    "voice",
    "breath",
    "scent",
    "hands",
    "skin",
    "chest",
    "lower_body",
    "neck",
    "build",
    "clothing",
    "accessory",
    "object",
    "nonhuman",
    "light",
    "sound",
    "weather",
)
_SUBJECT_LEVELS: tuple[str, ...] = ("absent", "action", "description")


def _subjects_from_logits(logits: Sequence[float]) -> dict[str, list[float]]:
    """Read the 60-logit head as ``{category: probs}``, *probs* a softmax in _SUBJECT_LEVELS order."""
    width = len(_SUBJECT_LEVELS)
    out: dict[str, list[float]] = {}
    for i, category in enumerate(_SUBJECT_CATEGORIES):
        row = logits[i * width : (i + 1) * width]
        top = max(row)
        exp = [math.exp(x - top) for x in row]
        total = sum(exp)
        out[category] = [x / total for x in exp]
    return out


async def aclassify_subjects(narration: str) -> dict[str, list[float]]:
    """One reply's narration -> ``{category: probs}`` for every _SUBJECT_CATEGORIES entry. One model call."""
    if not narration.strip():
        return {category: [1.0, 0.0, 0.0] for category in _SUBJECT_CATEGORIES}  # nothing to read: never load the model for it
    (logits,) = await _logits("subjects_classifier", [narration], len(_SUBJECT_CATEGORIES) * len(_SUBJECT_LEVELS))
    return _subjects_from_logits(logits)


# The subject pair comparer reads one (earlier reply, draft) pair as [CLS] + earlier part + draft part + [SEP], each part
# tokenized on its own and cut to _SUBJECT_PAIR_SIDE_IDS, exactly as training built it; n_ctx 2048 holds both sides. Its head is
# 20 logits in _SUBJECT_CATEGORIES order, one sigmoid each: the probability that the draft repeats that subject's description
# from the earlier reply.
_SUBJECT_PAIR_SIDE_IDS = 1000


def _subject_pair_ids(cls: int, sep: int, earlier_ids: Sequence[int], draft_ids: Sequence[int]) -> list[int]:
    """The comparer's input ids from each shaped part's own special-free ids (`subjects.subject_pair_parts`)."""
    return [cls, *earlier_ids[:_SUBJECT_PAIR_SIDE_IDS], *draft_ids[:_SUBJECT_PAIR_SIDE_IDS], sep]


def _subject_repeats_from_logits(logits: Sequence[float]) -> dict[str, float]:
    """Read the 20-logit pair head as ``{category: repeat probability}``."""
    return {category: 1.0 / (1.0 + math.exp(-x)) for category, x in zip(_SUBJECT_CATEGORIES, logits, strict=True)}


async def _pair_logits(pairs: Sequence[tuple[str, str]], n: int) -> list[list[float]]:
    async with _host(_SUBJECT_COMPARER).use(_head_profile(_SUBJECT_COMPARER)) as server:
        cls, sep = await _specials(server, _SUBJECT_COMPARER)
        parts = [part for pair in pairs for part in pair]
        ids = await _bounded([lambda part=part: server.tokenize(part) for part in parts])
        seqs = [_subject_pair_ids(cls, sep, ids[2 * i], ids[2 * i + 1]) for i in range(len(pairs))]
        return await server.head_logits(seqs, n)


async def acompare_subjects(pairs: Sequence[tuple[str, str]]) -> list[dict[str, float]]:
    """Per (earlier part, draft part), ``{category: repeat probability}`` for every _SUBJECT_CATEGORIES entry. One request."""
    if not pairs:
        return []  # nothing to read: never load the model for it
    return [_subject_repeats_from_logits(v) for v in await _pair_logits(list(pairs), len(_SUBJECT_CATEGORIES))]
