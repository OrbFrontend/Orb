"""``ExtensionCtx`` -- the capability-filtered JSON view of an invocation.

Never hand a community flow the trusted workflow context objects. ``PreCtx``
and ``PostCtx`` carry an LLM client, a KV tracker, a mutable turn scratch dict,
and a full settings snapshot that may hold endpoint URLs, API keys, and proxy
configuration. A package must not see any of it, and "must not read it" is a
weaker guarantee than "was never given it" -- so this module *builds* a new
plain-JSON dict rather than filtering or proxying an existing one.

Two properties follow from constructing rather than wrapping:

* **Absent by default.** A field appears only when the matching grant is in
  the approved set. There is no code path that copies a field and then decides
  whether to hide it, so a missing capability check cannot leak a field it
  forgot to guard -- the field simply never gets written.
* **A snapshot, not a view.** Every value is a fresh copy of a JSON scalar or
  container. A flow cannot reach live core state through it, and mutating what
  it received cannot affect the turn.

Identity fields (``extension_id``, ``hook``, ``conversation.id``,
``message.id``) carry no grant: they tell a flow who and where it is, which it
necessarily already knows, and the compiler's requirement derivation agrees --
only ``ctx.input``, ``ctx.draft``, ``ctx.history``, and ``ctx.character``
consume a capability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...core import normalize_tags
from .contracts import Capability
from .limits import (
    MAX_CTX_CHARACTER_BYTES,
    MAX_CTX_HISTORY_BYTES,
    MAX_CTX_HISTORY_MESSAGES,
    MAX_CTX_TEXT_BYTES,
)

CHARACTER_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "description",
    "personality",
    "scenario",
)
"""The allowlisted character projection.

An allowlist, not a denylist: ``avatar_b64``, ``avatar_mime``, ``extensions``
(raw V2 card extensions), ``system_prompt``, and ``post_history_instructions``
are excluded by not being named here, so a card gaining a column does not
silently gain a projected field. Avatar bytes and raw card extensions need
their own future capabilities; persona data is not card data and is absent from
every projection. The current tag list is appended separately because it is a
bounded array rather than a text field.
"""


def _clip(text: object, limit: int = MAX_CTX_TEXT_BYTES) -> str:
    """Coerce to text and clip to *limit* UTF-8 bytes on a character boundary.

    Clipping rather than rejecting: a 2 MiB draft is an ordinary long reply,
    not an attack, and failing the invocation over it would make a hook's
    reliability depend on how much the writer happened to produce. Encoding
    once and decoding with ``errors="ignore"`` is what keeps the cut off a
    multi-byte character's middle.
    """
    raw = text if isinstance(text, str) else ""
    encoded = raw.encode("utf-8")
    if len(encoded) <= limit:
        return raw
    return encoded[:limit].decode("utf-8", errors="ignore")


def _history_window(history: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The most recent messages as role/content pairs, within both bounds.

    Role and bounded text only: no attachments, workflow state, progressive
    fields, logs, reasoning, ids, or inactive branches. The window is taken
    from the *end* because recency is what a hook is almost always after, and
    the aggregate byte cap then trims from the far end of that window, so a
    package never receives a window whose newest message was the one dropped.
    """
    recent = list(history)[-MAX_CTX_HISTORY_MESSAGES:]
    projected: list[dict[str, str]] = []
    used = 0
    for message in reversed(recent):
        role = message.get("role")
        if role not in ("user", "assistant", "system"):
            continue
        content = _clip(message.get("content", ""))
        size = len(content.encode("utf-8"))
        if used + size > MAX_CTX_HISTORY_BYTES:
            break
        used += size
        projected.append({"role": role, "content": content})
    projected.reverse()
    return projected


def _character(card: Mapping[str, Any]) -> dict[str, Any]:
    """The allowlisted card fields, within the aggregate byte cap."""
    projected: dict[str, Any] = {}
    used = 0
    for field in CHARACTER_FIELDS:
        value = card.get(field)
        if not isinstance(value, str) or not value:
            continue
        text = _clip(value, MAX_CTX_CHARACTER_BYTES)
        size = len(text.encode("utf-8"))
        if used + size > MAX_CTX_CHARACTER_BYTES:
            break
        used += size
        projected[field] = text
    tags: list[str] = []
    for tag in normalize_tags(card.get("tags")):
        size = len(tag.encode("utf-8"))
        if used + size > MAX_CTX_CHARACTER_BYTES:
            break
        used += size
        tags.append(tag)
    projected["tags"] = tags
    return projected


def build_ctx(
    *,
    extension_id: str,
    hook: str,
    granted: frozenset[tuple[str, str | None]],
    conversation_id: str | None = None,
    message_id: int | None = None,
    card: Mapping[str, Any] | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    last_user_message: str | None = None,
    draft: str | None = None,
) -> dict[str, Any]:
    """Build the ``ctx`` namespace one invocation may read.

    *granted* is the live approved requirement set, expanded the same way the
    publisher expands it. Passing the expanded set rather than raw permission
    entries means this function and the entry-point gate answer "is this
    granted?" identically; two expansions would eventually disagree, and the
    direction they disagreed in would decide whether an ungranted field was
    projected.
    """

    def has(capability: Capability) -> bool:
        return (capability.value, None) in granted

    ctx: dict[str, Any] = {"extension_id": extension_id, "hook": hook}
    if conversation_id:
        ctx["conversation"] = {"id": conversation_id}
    if message_id is not None:
        ctx["message"] = {"id": message_id}
    if last_user_message is not None and has(Capability.CONTEXT_INPUT_READ):
        ctx["input"] = {"last_user_message": _clip(last_user_message)}
    if draft is not None and has(Capability.CONTEXT_DRAFT_READ):
        ctx["draft"] = _clip(draft)
    if history is not None and has(Capability.CONTEXT_HISTORY_READ):
        ctx["history"] = _history_window(history)
    if card and has(Capability.CONTEXT_CHARACTER_READ):
        ctx["character"] = _character(card)
    return ctx
