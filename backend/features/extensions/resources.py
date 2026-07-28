"""Host resources: bounded, allowlisted projections a view or a package may read.

A resource is *not* a query primitive handed to a flow, and it is not an API
response passed through. It is a projection built for the extension surface,
served by an adapter that owns the fields, the bounds, and the grant check. A
package names a resource; it never names a table, a column, a filter, a sort,
or a page size.

Five conditions gate admitting a new one, and every resource here satisfies all
five:

1. It is an allowlisted field projection built for this surface.
2. It is bounded by both an item count and an encoded-byte budget, and
   paginated with an opaque host-owned cursor when the underlying set is
   unbounded.
3. Its scope is fixed by the invocation's own context unless a separate
   enumeration grant conspicuously covers wider reach. Only ``library.cards``
   has that wider reach, and ``library.cards.read`` is the grant that says so.
4. It is a database projection plus an adapter, never a query primitive.
5. Its consent line names what it reads in user terms.

The tree is the one resource that *fails* past its budget instead of
paginating, because a partial graph looks complete to whoever draws it. Every
other resource walks pages: truncating a library sweep would make it report
success over cards it never saw, and refusing outright would lock a large
library out of the feature with no recourse.

Cursors are opaque in the protocol sense and host-owned: a package receives a
token and must return it unchanged rather than constructing a position. The
token is authenticated, not encrypted; its MAC makes a mutated or
package-authored cursor invalid rather than letting it resolve to *some*
position.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...core import normalize_tags, resolve_persona_id
from ...database import (
    get_character_card,
    get_conversation,
    get_conversation_tree,
    get_direction_notes_for_path,
    get_lorebook_entries,
    get_settings,
    get_user_persona,
    list_library_cards,
)
from .compiler import Requirement
from .contracts import RESOURCE_CAPABILITIES, Capability
from .digest import canonical_json_bytes
from .errors import PackageError
from .limits import (
    MAX_CTX_PERSONA_BYTES,
    MAX_RESOURCE_BYTES,
    MAX_RESOURCE_PAGE_ITEMS,
    MAX_RESOURCE_TEXT_BYTES,
    MAX_TREE_NODES,
    MAX_TREE_PREVIEW_CHARS,
)

RESOURCE_NAMES: frozenset[str] = frozenset(RESOURCE_CAPABILITIES)


class ResourceError(PackageError):
    """A resource request that cannot be served, with a sanitized reason."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


class ResourceForbidden(ResourceError):
    def __init__(self, capability: str, parameter: str | None = None):
        grant = capability if parameter is None else f"{capability} for {parameter!r}"
        super().__init__(f"this extension has not been granted {grant}", status=403)


class ResourceTooLarge(ResourceError):
    def __init__(self, message: str):
        super().__init__(message, status=413)


# ── opaque cursors ───────────────────────────────────────────────────────────

_CURSOR_KEY = secrets.token_bytes(32)
"""Per-process MAC key for pagination cursors.

Regenerated on restart, which invalidates in-flight cursors -- acceptable,
because a walk that spans a restart has to refetch anyway, and the alternative
is a persisted key whose only job is to authenticate a value the client just
received from us."""


def encode_cursor(resource: str, payload: Mapping[str, Any]) -> str:
    """Wrap a position as an opaque, MAC'd, resource-bound token."""
    body = canonical_json_bytes({"r": resource, "p": dict(payload)})
    mac = hmac.new(_CURSOR_KEY, body, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(mac + body).decode("ascii").rstrip("=")


def decode_cursor(resource: str, token: str | None) -> dict[str, Any]:
    """Unwrap a cursor, or refuse it. ``None`` means "start of the walk".

    Bound to the resource that issued it as well as to the key: a
    ``library.cards`` cursor replayed against the tree resource is rejected
    rather than reinterpreted, so an opaque token cannot be smuggled sideways
    into a different projection's position argument.
    """
    if token is None:
        return {}
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        mac, body = raw[:16], raw[16:]
        expected = hmac.new(_CURSOR_KEY, body, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(mac, expected):
            raise ValueError
        decoded = json.loads(body)
        if not isinstance(decoded, dict) or decoded.get("r") != resource:
            raise ValueError
        position = decoded.get("p")
        if not isinstance(position, dict):
            raise ValueError
        return position
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise ResourceError("the pagination cursor is not one this server issued") from None


# ── request ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    """One resolved resource read: who is asking, for what, from where."""

    extension_id: str
    resource: str
    granted: frozenset[Requirement]
    conversation_id: str | None = None
    cursor: str | None = None

    def has(self, capability: Capability, parameter: str | None = None) -> bool:
        return (capability.value, parameter) in self.granted

    def require(self, capability: str, parameter: str | None = None) -> None:
        if (capability, parameter) not in self.granted:
            raise ResourceForbidden(capability, parameter)


async def resolve_resource(request: ResourceRequest) -> dict[str, Any]:
    """Serve one host resource, or raise a :class:`ResourceError`.

    The grant check happens here rather than in the route so the view loader
    and the HTTP route cannot drift: both call this, and neither can serve a
    projection this function refused.
    """
    handler = _HANDLERS.get(request.resource)
    if handler is None:
        raise ResourceError(f"unknown host resource {request.resource!r}", status=404)
    request.require(*RESOURCE_CAPABILITIES[request.resource])
    payload = await handler(request)
    _assert_within_budget(payload, request.resource)
    return payload


def _assert_within_budget(payload: Any, resource: str) -> None:
    size = len(canonical_json_bytes(payload))
    if size > MAX_RESOURCE_BYTES:
        raise ResourceTooLarge(f"the {resource!r} projection is {size} bytes, over the {MAX_RESOURCE_BYTES} byte budget")


def _clip(text: object, limit: int = MAX_RESOURCE_TEXT_BYTES) -> str:
    raw = text if isinstance(text, str) else ""
    encoded = raw.encode("utf-8")
    return raw if len(encoded) <= limit else encoded[:limit].decode("utf-8", errors="ignore")


def _bounded_strings(values: object, *, byte_limit: int = MAX_RESOURCE_TEXT_BYTES) -> list[str]:
    """Project an untrusted string list under one aggregate UTF-8 budget."""
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    remaining = byte_limit
    for value in values[:MAX_RESOURCE_PAGE_ITEMS]:
        if not isinstance(value, str):
            continue
        clipped = _clip(value, remaining)
        result.append(clipped)
        remaining -= len(clipped.encode("utf-8"))
        if remaining <= 0:
            break
    return result


def _cursor_after(resource: str, cursor: str | None) -> int:
    position = decode_cursor(resource, cursor)
    after = position.get("after", 0)
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise ResourceError(f"the {resource!r} pagination cursor has an invalid position")
    return after


def _bounded_item_page(
    resource: str,
    field: str,
    items: Sequence[dict[str, Any]],
    *,
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit a cursor page to both the item and serialized-byte ceilings."""
    prefix = dict(base or {})
    emitted: list[dict[str, Any]] = []
    for item in items[:MAX_RESOURCE_PAGE_ITEMS]:
        candidate_items = [*emitted, item]
        has_more = len(candidate_items) < len(items)
        candidate = {
            **prefix,
            field: candidate_items,
            "next_cursor": encode_cursor(resource, {"after": item["id"]}) if has_more else None,
        }
        if len(canonical_json_bytes(candidate)) > MAX_RESOURCE_BYTES:
            break
        emitted.append(item)

    if items and not emitted:
        raise ResourceTooLarge(f"one {resource!r} item is too large for the bounded extension projection")

    has_more = len(emitted) < len(items)
    return {
        **prefix,
        field: emitted,
        "next_cursor": encode_cursor(resource, {"after": emitted[-1]["id"]}) if has_more and emitted else None,
    }


async def _require_conversation(request: ResourceRequest) -> Mapping[str, Any]:
    """The invocation's conversation, or a refusal.

    A resource whose scope is "the invocation's conversation" must fail when
    there is no conversation rather than fall back to *some* conversation --
    falling back is how an action run from a library workspace would silently
    read whichever chat happened to be open.
    """
    if not request.conversation_id:
        raise ResourceError(f"the {request.resource!r} resource needs a conversation, and this invocation has none")
    conv = await get_conversation(request.conversation_id)
    if conv is None:
        raise ResourceError("conversation not found", status=404)
    return conv


# ── conversation.tree ────────────────────────────────────────────────────────


async def _conversation_tree(request: ResourceRequest) -> dict[str, Any]:
    conv = await _require_conversation(request)
    previews = MAX_TREE_PREVIEW_CHARS if request.has(Capability.CONVERSATION_TREE_READ, "preview") else 0
    try:
        tree = await get_conversation_tree(str(conv["id"]), max_nodes=MAX_TREE_NODES, preview_chars=previews)
    except ValueError as exc:
        # Explicitly too large, never a partial graph: a tree view that received
        # half the nodes would draw a conversation the user does not have.
        raise ResourceTooLarge(str(exc)) from None
    if tree is None:
        raise ResourceError("conversation not found", status=404)
    return tree


# ── library.cards ────────────────────────────────────────────────────────────


async def _library_cards(request: ResourceRequest) -> dict[str, Any]:
    position = decode_cursor("library.cards", request.cursor)
    after = position.get("after", 0)
    snapshot = position.get("snapshot")
    created_before = position.get("created_before")
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise ResourceError("the library pagination cursor has an invalid position")
    if snapshot is not None and (isinstance(snapshot, bool) or not isinstance(snapshot, int) or snapshot < after):
        raise ResourceError("the library pagination cursor has an invalid snapshot")
    if (snapshot is None) != (created_before is None) or (
        created_before is not None and (not isinstance(created_before, str) or not created_before)
    ):
        raise ResourceError("the library pagination cursor has an invalid snapshot boundary")

    page, snapshot, created_before = await list_library_cards(
        request.extension_id,
        after_rowid=after,
        snapshot_rowid=snapshot,
        snapshot_created_at=created_before,
        limit=MAX_RESOURCE_PAGE_ITEMS + 1,
    )
    # The extension's own slot rides the enumeration grant only when it may also
    # read its state at all. Without it the field is absent rather than empty:
    # "you did not grant state.read" and "this card has no record yet" are
    # different answers, and a sweep's "already classified" test depends on
    # telling them apart.
    include_state = request.has(Capability.STATE_READ, "character")
    cards: list[dict[str, Any]] = []
    last_rowid = after
    for row in page[:MAX_RESOURCE_PAGE_ITEMS]:
        card = {
            "id": row["id"],
            "name": _clip(row["name"]),
            # Imports deliberately preserve author fidelity in storage, so the
            # read projection must impose its own bounds instead of assuming
            # every historical row passed through the write normalizer.
            "tags": normalize_tags(row["tags"]),
            **({"state": row["state"]} if include_state else {}),
        }
        rowid = row["cursor_rowid"]
        candidate_cursor = encode_cursor(
            "library.cards",
            {"after": rowid, "snapshot": snapshot, "created_before": created_before},
        )
        candidate = {"cards": [*cards, card], "next_cursor": candidate_cursor}
        if len(canonical_json_bytes(candidate)) > MAX_RESOURCE_BYTES:
            break
        cards.append(card)
        last_rowid = rowid

    if page and not cards:
        # With normalized tags, a clipped name, and a state slot capped below
        # half the response budget, a valid row fits. Keep the refusal explicit
        # for corrupt legacy state instead of returning an empty page whose
        # unchanged cursor would make the renderer loop forever.
        raise ResourceTooLarge("one library card is too large for the bounded extension projection")

    has_more = len(cards) < len(page)
    return {
        "cards": cards,
        "next_cursor": (
            encode_cursor(
                "library.cards",
                {"after": last_rowid, "snapshot": snapshot, "created_before": created_before},
            )
            if has_more and cards
            else None
        ),
    }


# ── lorebook.entries ─────────────────────────────────────────────────────────


async def _lorebook_entries(request: ResourceRequest) -> dict[str, Any]:
    """The lorebook of the world bound to the invocation's conversation.

    No world enumeration: that would be a ``library.cards.read``-shaped reach
    grant, and it waits for a package that needs it. Note the hazard the
    consent banner exists for -- untriggered entries are content the model may
    never have seen, so this is a stronger read than the history window when
    combined with network access.
    """
    conv = await _require_conversation(request)
    world_id = conv.get("world_id")
    if not world_id:
        card_id = conv.get("character_card_id")
        card = await get_character_card(str(card_id)) if card_id else None
        world_id = card.get("world_id") if card else None
    if not world_id:
        return {"world_id": None, "entries": [], "next_cursor": None}

    after = _cursor_after("lorebook.entries", request.cursor)
    rows = [row for row in await get_lorebook_entries(str(world_id)) if row["id"] > after]
    rows.sort(key=lambda row: row["id"])
    entries = [
        {
            "id": row["id"],
            "name": _clip(row.get("name")),
            "keys": _bounded_strings(row.get("keywords")),
            "secondary_keys": _bounded_strings(row.get("secondary_keys")),
            "selective": bool(row.get("selective")),
            "use_regex": bool(row.get("use_regex")),
            "enabled": bool(row.get("enabled")),
            "insertion_order": row.get("sort_order", 0),
            "content": _clip(row.get("content")),
        }
        for row in rows
    ]
    return _bounded_item_page("lorebook.entries", "entries", entries, base={"world_id": world_id})


# ── direction.notes ──────────────────────────────────────────────────────────


async def _direction_notes(request: ResourceRequest) -> dict[str, Any]:
    """The active branch's direction notes. Read-only, permanently.

    Notes are injected into prompts, so a write here would be a first-party
    write that reaches generation -- a different threat model needing its own
    review, not one more entry in an allowlist.
    """
    conv = await _require_conversation(request)
    tree = await get_conversation_tree(str(conv["id"]), max_nodes=MAX_TREE_NODES, preview_chars=0)
    path = list(tree["active_path"]) if tree else []
    rows = await get_direction_notes_for_path(str(conv["id"]), path)

    after = _cursor_after("direction.notes", request.cursor)
    ordered = sorted((row for row in rows if row["id"] > after), key=lambda row: row["id"])
    notes = [
        {
            "id": row["id"],
            "content": _clip(row.get("content")),
            # There is no author column. A Director-recorded note carries the
            # fragment it came from; a user-authored one does not, so the
            # presence of that id *is* the author kind. Projecting the derived
            # word rather than the fragment id keeps the package out of the
            # business of knowing which fragments exist.
            "author": "director" if row.get("interactive_fragment_id") else "user",
            "created_at": row.get("created_at"),
        }
        for row in ordered
    ]
    return _bounded_item_page("direction.notes", "notes", notes)


# ── persona ──────────────────────────────────────────────────────────────────


async def _persona(request: ResourceRequest) -> dict[str, Any]:
    """The active persona's name and description, byte-capped.

    This is the user's own self-description, which is why it is a separate
    conspicuous grant rather than part of the character projection, and why it
    is the strongest trigger for the consent combination banner. Never
    writable.
    """
    conv = await _require_conversation(request)
    settings = await get_settings()
    card_id = conv.get("character_card_id")
    card = await get_character_card(str(card_id)) if card_id else None
    persona_id = resolve_persona_id(conv, card, settings)
    persona = await get_user_persona(int(persona_id)) if persona_id else None
    if persona is None:
        return {"persona": None}
    name = _clip(persona.get("name"), MAX_CTX_PERSONA_BYTES)
    description_budget = MAX_CTX_PERSONA_BYTES - len(name.encode("utf-8"))
    return {
        "persona": {
            "name": name,
            "description": _clip(persona.get("description"), description_budget),
        }
    }


_HANDLERS: dict[str, Any] = {
    "conversation.tree": _conversation_tree,
    "library.cards": _library_cards,
    "lorebook.entries": _lorebook_entries,
    "direction.notes": _direction_notes,
    "persona": _persona,
}

_UNHANDLED = RESOURCE_NAMES - set(_HANDLERS)
assert not _UNHANDLED, f"host resources without an adapter: {sorted(_UNHANDLED)}"


def resources_needing_conversation() -> frozenset[str]:
    """Resources whose scope is fixed by the invocation's conversation."""
    return frozenset({"conversation.tree", "lorebook.entries", "direction.notes", "persona"})


def data_sources_for_view(view: Any) -> Sequence[tuple[str, Any]]:
    """``(name, source)`` pairs in deterministic order, for the view loader."""
    return sorted(view.data.items())
