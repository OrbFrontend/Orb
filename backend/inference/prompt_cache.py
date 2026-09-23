"""Explicit prompt-cache breakpoints and upstream affinity for chat requests.

Anthropic-style caches -- Claude on its own API or behind OpenRouter and
NanoGPT, Gemini and Qwen behind OpenRouter -- store nothing unless the request
marks where a reusable prefix ends, and an entry is found again only at a
position some earlier request marked. Orb's calls share a stable base and differ
in a per-call tail, so a lone marker on the final block (what top-level
automatic caching does) writes the whole prompt on every call and never reads
it back: the next call's tail is different. Measured on Claude through both
routers, that shape read 0 tokens across passes and turns. The anchors sit
where Orb's prompts actually repeat instead:

- the system prompt, 1 h -- survives an edited history and serves batch lanes;
- the last message of the shared base, 1 h -- the cross-pass and cross-turn
  anchor; the next turn's base extends it and the provider's lookback finds it;
- the final block, 5 min -- the Editor extends the Writer's request, and its
  ReAct iterations extend each other, all within the turn.

Longer TTLs precede shorter ones, as Anthropic requires. The 1 h entries cover
a reader's pause between turns: a miss there re-writes the whole conversation,
while the longer TTL only doubles the price of each turn's new delta.

Servers that do their own prefix caching ignore the markers, and a one-part text
list renders to the same bytes as the string it replaces (llama.cpp's template
output, and every hosted route's prompt token count, agree), so a marker moving
from message to message never moves a byte of the rendered prompt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

_LONG: dict[str, str] = {"type": "ephemeral", "ttl": "1h"}
_SHORT: dict[str, str] = {"type": "ephemeral"}


def _has_text(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        return any(_is_text_part(part) for part in content)
    return False


def _is_text_part(part: object) -> bool:
    return isinstance(part, Mapping) and part.get("type") == "text" and bool(str(part.get("text") or "").strip())


def _mark(message: Mapping[str, Any], control: Mapping[str, str]) -> dict[str, Any]:
    """Copy *message* with *control* on its last non-empty text part."""
    content = message["content"]
    if isinstance(content, str):
        return {**message, "content": [{"type": "text", "text": content, "cache_control": dict(control)}]}
    parts = list(content)
    for index in range(len(parts) - 1, -1, -1):
        if _is_text_part(parts[index]):
            parts[index] = {**parts[index], "cache_control": dict(control)}
            break
    return {**message, "content": parts}


def mark_cache_breakpoints(messages: Sequence[Mapping[str, Any]], prefix_len: int | None) -> list[Mapping[str, Any]]:
    """Return *messages* with cache breakpoints at the system, base, and tail ends.

    *prefix_len* is how many leading messages every call on this base shares;
    ``None`` leaves only the system and tail anchors. Each anchor walks back to
    the nearest message with text (Anthropic rejects empty text blocks), never
    past the previous anchor. Changed messages are copies; the caller's
    transcript is untouched.
    """
    out = list(messages)
    system_end = 0
    while system_end < len(out) and out[system_end].get("role") == "system":
        system_end += 1
    base_end = min(prefix_len or 0, len(out))
    floor = 0
    for end, control in ((system_end, _LONG), (base_end, _LONG), (len(out), _SHORT)):
        if end <= floor:
            continue
        for index in range(end - 1, floor - 1, -1):
            if _has_text(out[index]):
                out[index] = _mark(out[index], control)
                break
        floor = end
    return out


def affinity_headers(model: str, messages: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Name the request's cache lane so a router keeps it on one upstream.

    OpenRouter spreads a model across upstreams, each with its own cache, and
    without a session id it only turns sticky after a cache hit -- which a
    lane hopping upstream on every call may never get. The id is derived from
    the model and the lane's first message, so every pass and turn of one
    conversation shares it. Servers that do not read the header ignore it.
    """
    if not messages:
        return {}
    seed = json.dumps([model, messages[0]], sort_keys=True, ensure_ascii=False, default=str)
    return {"x-session-id": hashlib.sha256(seed.encode()).hexdigest()[:32]}
