"""Pure helpers for LLMClient's text-completion transport (llama.cpp).

Text mode renders messages via ``POST /apply-template`` then streams
``POST /completion`` — llama.cpp's native endpoints, beside the OpenAI-compat
``/v1`` surface. This module holds everything that needs no socket, so it is
unit-testable without mocking HTTP: the reasoning think-tag splitter, the
``/props`` template-tag sniff (+ a session cache), hyperparameter remapping,
usage synthesis, forced-schema lookup, and image-part detection. ``client.py``
owns the sockets, abort race, and SSE loop; this module owns the shapes.

Mirrors the HTTP-free leaf pattern of ``gemma_tool_format.py`` /
``endpoint_profiles.py``: no imports from ``client`` (no cycle).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reasoning think-tag triple: (open, close, disable_suffix).
#
# ``open``/``close`` bound the reasoning span the model emits in the stream.
# ``disable_suffix`` appended to the rendered prompt forces reasoning off (the
# template's own "empty thought channel" bytes; probe-verified on llama.cpp).
# ---------------------------------------------------------------------------
ThinkTags = tuple[str, str, str]

# Gemma-4 emits reasoning inside a channel pair; the disable bytes are the
# open channel immediately closed. Probe-verified (2026-07-04, Gemma 4 31B).
_GEMMA4: ThinkTags = ("<|channel>thought\n", "<channel|>", "<|channel>thought\n<channel|>")
# Qwen/DeepSeek-style <think></think> pair; disable is an empty think block.
_THINK: ThinkTags = ("<think>", "</think>", "<think>\n\n</think>\n\n")
# Non-thinking model: no span, no-op suffix (reasoning toggle does nothing).
_NONE: ThinkTags = ("", "", "")


def think_tags_from_template(chat_template: str) -> ThinkTags:
    """Sniff the reasoning-tag triple from a server's ``chat_template`` text.

    Gemma-4 channel pair wins over ``<think>`` when both markers appear (a
    template can mention both). Neither present => non-thinking model.
    """
    if "<|channel>thought" in chat_template:
        return _GEMMA4
    if "<think>" in chat_template:
        return _THINK
    return _NONE


# Session cache keyed by server root (clients are rebuilt every turn, so an
# instance cache would re-probe /props per turn). Mirrors endpoint_profiles'
# session-learned registry. Only successful sniffs are cached, so a transient
# /props failure self-heals on the next call.
_tag_cache: dict[str, ThinkTags] = {}


async def get_think_tags(server_root: str, fetch_template: Callable[[], Awaitable[str]]) -> ThinkTags:
    """Return the cached tag triple for *server_root*, sniffing on a miss.

    *fetch_template* is an async ``() -> chat_template str`` supplied by the
    client (it owns the HTTP). On fetch failure the caller's callable should
    return ``""``; we then fall back to :data:`_NONE` for this call **without**
    caching, so a later call retries the probe.
    """
    cached = _tag_cache.get(server_root)
    if cached is not None:
        return cached
    template = await fetch_template()
    tags = think_tags_from_template(template)
    if template:  # only cache a real sniff; let a failed /props retry next call
        _tag_cache[server_root] = tags
    return tags


def _max_overlap(buf: str, target: str) -> int:
    """Length of the longest suffix of *buf* that is a (proper) prefix of *target*.

    Used to hold back a possible tag split across chunk boundaries. A full match
    is handled by ``str.find`` before this is reached, so the answer is at most
    ``len(target) - 1``.
    """
    m = min(len(buf), len(target) - 1)
    for k in range(m, 0, -1):
        if target.startswith(buf[-k:]):
            return k
    return 0


def _scan(buf: str, target: str) -> tuple[str, str, bool]:
    """Split *buf* against *target*.

    Returns ``(emit, remainder, matched)``:
      - *target* found: ``emit`` is the text before it, ``remainder`` the text
        after it, ``matched=True``.
      - else: hold back the longest tail of *buf* that could be a split *target*;
        ``emit`` is the rest, ``remainder`` the held tail, ``matched=False``.
    """
    i = buf.find(target)
    if i != -1:
        return buf[:i], buf[i + len(target) :], True
    k = _max_overlap(buf, target)
    if k:
        return buf[:-k], buf[-k:], False
    return buf, "", False


class ThinkSplitter:
    """Stateful reasoning/content splitter over a text-completion token stream.

    ``feed(delta)`` returns a list of ``(kind, text)`` pairs where *kind* is
    ``"reasoning"`` or ``"content"``. It holds back partial tag-prefixes at
    chunk boundaries so a tag split across SSE chunks (llama.cpp streams
    ``"<|channel>"`` ``"thought"`` ``"\\n"`` as three pieces) is not
    misclassified. ``flush()`` drains any held tail at end of stream.

    States: ``pre`` (before reasoning; provisionally content — a model that
    never opens a thought channel stays here and everything is content),
    ``reasoning`` (inside the span), ``content`` (after the span; no more tags).
    A non-thinking model (empty open tag) starts in ``content``.

    ``already_open`` starts in ``reasoning`` instead of ``pre``: some chat
    templates (Qwen3) emit the *opening* think tag in the generation prompt, so
    the model's stream begins *inside* the span with no open tag to see. The
    caller sets this by inspecting the rendered prompt (see ``_complete_text``).
    Templates that leave the open tag to the model's output (Gemma 4) pass
    ``False`` and the default ``pre`` scan catches it.
    """

    def __init__(self, tags: ThinkTags, already_open: bool = False) -> None:
        self._open, self._close, _ = tags
        self._buf = ""
        if not self._open:
            self._state = "content"
        elif already_open:
            self._state = "reasoning"
        else:
            self._state = "pre"

    def feed(self, delta: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._buf += delta
        while True:
            if self._state == "content":
                if self._buf:
                    out.append(("content", self._buf))
                    self._buf = ""
                break
            target = self._open if self._state == "pre" else self._close
            kind = "content" if self._state == "pre" else "reasoning"
            emit, rem, matched = _scan(self._buf, target)
            if emit:
                out.append((kind, emit))
            self._buf = rem
            if not matched:
                break
            self._state = "reasoning" if self._state == "pre" else "content"
        return out

    def flush(self) -> list[tuple[str, str]]:
        """Emit any held tail as the current state's kind (reasoning if mid-span)."""
        if not self._buf:
            return []
        kind = "reasoning" if self._state == "reasoning" else "content"
        out = [(kind, self._buf)]
        self._buf = ""
        return out


def reasoning_enabled(params: Mapping[str, Any]) -> bool:
    """Read the per-call reasoning flag from ``reasoning_cfg``-style params.

    Defaults to ``True`` (thinking on) when no reasoning hint is present, matching
    the templates' default render.
    """
    ctk = params.get("chat_template_kwargs")
    if isinstance(ctk, dict) and "enable_thinking" in ctk:
        return bool(ctk["enable_thinking"])
    think = params.get("thinking")
    if isinstance(think, dict) and think.get("type") == "disabled":
        return False
    return True


# Hyperparams /completion accepts unchanged.
_PASSTHROUGH = ("temperature", "top_p", "top_k", "min_p")


def build_completion_params(params: Mapping[str, Any]) -> dict:
    """Remap chat-completion hyperparams to a ``/completion`` request body.

    Renames ``max_tokens``->``n_predict`` and ``repetition_penalty``->
    ``repeat_penalty``; passes temperature/top_p/top_k/min_p through; adds
    ``cache_prompt: true``. Everything else (reasoning/thinking/
    chat_template_kwargs/stream_options/prefill/...) is dropped by omission —
    this is an allowlist.
    """
    out: dict[str, Any] = {"cache_prompt": True}
    for k in _PASSTHROUGH:
        v = params.get(k)
        if v is not None:
            out[k] = v
    if params.get("max_tokens") is not None:
        out["n_predict"] = params["max_tokens"]
    if params.get("repetition_penalty") is not None:
        out["repeat_penalty"] = params["repetition_penalty"]
    return out


def has_image_parts(messages: Sequence[Mapping[str, Any]]) -> bool:
    """True if any message's content is a parts list containing an ``image_url``.

    Text mode can't render images (no multimodal /apply-template path yet), so
    such a call routes back through the chat transport — same server + template,
    so the KV cache stays warm.
    """
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def forced_schema(tools: Sequence[Mapping[str, Any]] | None, tool_choice: Any) -> dict | None:
    """Return the JSON schema to grammar-constrain a forced tool call, or ``None``.

    *tool_choice* is Orb's only forced shape:
    ``{"type":"function","function":{"name":X}}``. Looks *X* up in *tools* and
    returns its ``function.parameters``. ``"required"``/``"auto"``/``"none"``/
    ``None`` -> ``None`` (no grammar; the ``parse_tool_calls`` chain handles any
    calls the model makes on its own).
    """
    if not isinstance(tool_choice, dict) or not tools:
        return None
    name = (tool_choice.get("function") or {}).get("name")
    if not name:
        return None
    for t in tools:
        fn = t.get("function") or {}
        if fn.get("name") == name:
            return fn.get("parameters") or {}
    return None


def synthesize_usage(final: Mapping[str, Any]) -> dict:
    """Build an OpenAI-shaped ``usage`` dict from a ``/completion`` final chunk.

    Provider-truth, exact (probe-verified F8): ``prompt_tokens`` =
    ``tokens_evaluated``, ``completion_tokens`` = ``tokens_predicted``,
    ``cached_tokens`` = ``tokens_evaluated - timings.prompt_n`` (the prefix the
    server reused). Consumed unchanged by the KV tracker's ``extract_cache_stats``.
    """
    evaluated = int(final.get("tokens_evaluated") or 0)
    predicted = int(final.get("tokens_predicted") or 0)
    prompt_n = int((final.get("timings") or {}).get("prompt_n") or 0)
    cached = max(0, evaluated - prompt_n)
    return {
        "prompt_tokens": evaluated,
        "completion_tokens": predicted,
        "total_tokens": evaluated + predicted,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def forced_tool_message(name: str, arguments: str) -> dict:
    """Assemble the ``done`` message for a grammar-forced tool call.

    Byte-symmetric with chat mode: empty content, one ``tool_calls`` entry whose
    ``arguments`` is the raw JSON string the grammar produced. It flows through
    the existing ``json.loads`` path in ``parse_tool_calls`` unchanged.
    """
    return {
        "content": "",
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }
