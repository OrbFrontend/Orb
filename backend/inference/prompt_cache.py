"""Explicit prompt-cache breakpoints and upstream affinity for chat requests.

Claude-style caches find an entry only at a position some earlier request
marked, and Orb's per-call tail means the last block never repeats, so the
breakpoints sit where prompts do repeat: the system prompt and the end of the
shared base (1 h), plus the final block (5 min) for requests that extend this
one within the turn. See "Providers that cache only where asked" in
docs/architecture/kv-cache.md.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

_LONG: dict[str, str] = {"type": "ephemeral", "ttl": "1h"}
_SHORT: dict[str, str] = {"type": "ephemeral"}


def _last_text(message: Mapping[str, Any]) -> int | None:
    """Index of the part a marker can go on: ``-1`` for string content, ``None`` if no text.

    Anthropic rejects empty text blocks, so whitespace does not count.
    """
    content = message.get("content")
    if isinstance(content, str):
        return -1 if content.strip() else None
    if not isinstance(content, Sequence):
        return None
    for index in range(len(content) - 1, -1, -1):
        part = content[index]
        if isinstance(part, Mapping) and part.get("type") == "text" and str(part.get("text") or "").strip():
            return index
    return None


def mark_cache_breakpoints(messages: Sequence[Mapping[str, Any]], prefix_len: int | None) -> list[Mapping[str, Any]]:
    """Return *messages* with cache breakpoints at the system, base, and tail ends.

    *prefix_len* is how many leading messages every call on this base shares;
    ``None`` leaves only the system and tail anchors. Each anchor walks back to
    the nearest message with text, never past the previous anchor, and TTLs only
    shorten from one anchor to the next, as Anthropic requires. A marked string
    becomes a one-part text list, which renders to the same bytes. Changed
    messages are copies; the caller's transcript is untouched.
    """
    out = list(messages)
    system_end = 0
    while system_end < len(out) and out[system_end].get("role") == "system":
        system_end += 1
    floor = 0
    for end, control in ((system_end, _LONG), (min(prefix_len or 0, len(out)), _LONG), (len(out), _SHORT)):
        for index in range(end - 1, floor - 1, -1):
            part = _last_text(out[index])
            if part is None:
                continue
            content = out[index]["content"]
            parts = [{"type": "text", "text": content}] if part == -1 else list(content)
            parts[part] = {**parts[part], "cache_control": dict(control)}
            out[index] = {**out[index], "content": parts}
            break
        floor = max(floor, end)
    return out


def affinity_headers(model: str, messages: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Name the request's cache lane so a router keeps it on one upstream.

    OpenRouter spreads a model across upstreams with separate caches, and
    without a session id it turns sticky only after a cache hit, which a lane
    changing upstream on every call may never get. The id comes from the model
    and the lane's first message, so every pass and turn of a conversation
    shares it.
    """
    if not messages:
        return {}
    seed = json.dumps([model, messages[0]], sort_keys=True, ensure_ascii=False, default=str)
    return {"x-session-id": hashlib.sha256(seed.encode()).hexdigest()[:32]}
