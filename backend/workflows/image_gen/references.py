"""Resolve a workflow's mapped `LoadImage` slots to actual reference bytes.

Sits at the `image_gen` top level rather than under `engine/`: this reads
conversation state through the workflow toolkit, while `engine/` stays
ComfyUI-only. Uploading and patching are the engine's half of the split.

Two entry points, because the two render routes make different promises.
`resolve_references` picks what a *fresh* render should use from the branch as it
stands; `refetch_references` re-fetches strictly by recorded origin, so a reroll
changes only the seed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from ..toolkit import (
    EVICTED_MARKER,
    get_character_avatar,
    get_user_attachments_for_message,
    get_workflow_attachment_by_id,
    get_workflow_character_state,
)
from .config import REFERENCE_SOURCES, WORKFLOW_ID, normalize_profile
from .engine import ImageGenerationError
from .engine.contracts import ResolvedReference
from .engine.display_encode import normalize_reference

# How each source reads in an error message. The names the import picker uses.
SOURCE_LABELS = {
    "previous": "the previous image in the chat",
    "character": "the character reference image",
}


def _bytes_from_row(row: Mapping[str, Any] | None) -> tuple[bytes, str] | None:
    """Decoded image bytes off an attachment row, or None when unusable.

    An evicted row holds the sentinel rather than base64; both that and a row
    that does not decode fall through to the next candidate rather than raising,
    so one bad row cannot block a walk-back with images left to try.
    """
    payload = (row or {}).get("data_b64")
    mime = (row or {}).get("mime_type")
    if not isinstance(payload, str) or not payload or payload == EVICTED_MARKER:
        return None
    if not isinstance(mime, str) or not mime.startswith("image/"):
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError):
        return None
    return (data, mime) if data else None


def _rows(message: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    rows = message.get(key)
    return [row for row in (rows if isinstance(rows, (list, tuple)) else []) if isinstance(row, Mapping)]


def _active_generated_image(message: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The image_gen attachment this message is actually *showing*.

    Reroll siblings share a group root whose `active_sibling_id` names the one on
    screen (NULL means the newest), and the latest group wins. Same rule the chat
    renderer uses, so a reference is the image the user is looking at.
    """
    ours = [a for a in _rows(message, "workflow_attachments") if a.get("workflow_id") == WORKFLOW_ID]
    if not ours:
        return None
    by_id = {a.get("id"): a for a in ours}

    def root(attachment: Mapping[str, Any]) -> Any:
        parent = attachment.get("parent_attachment_id")
        return parent if parent is not None and parent in by_id else attachment.get("id")

    newest = max((root(a) for a in ours), key=lambda key: (key is not None, key))
    siblings = sorted((a for a in ours if root(a) == newest), key=lambda a: a.get("id") or 0)
    active_id = (by_id.get(newest) or {}).get("active_sibling_id")
    return next((s for s in siblings if s.get("id") == active_id), siblings[-1])


def _uploaded_image(message: Mapping[str, Any]) -> Mapping[str, Any] | None:
    uploads = [a for a in _rows(message, "user_attachments") if str(a.get("mime_type") or "").startswith("image/")]
    return uploads[-1] if uploads else None


def _previous_image(history: Sequence[Mapping[str, Any]], anchor_id: int) -> tuple[bytes, str, str] | None:
    """The newest usable image on this branch before the anchor.

    The anchor is excluded: a regenerate would otherwise feed the image already
    attached to the message back in as its own reference, editing the previous
    render instead of the scene. A user's own upload counts -- "edit this photo
    of me" is the same request -- and its origin carries the message id too,
    since user attachments are only readable per-message.
    """
    for message in reversed(list(history)):
        if message.get("id") == anchor_id:
            continue
        for row, prefix in (
            (_active_generated_image(message), "attachment"),
            (_uploaded_image(message), f"upload:{message.get('id')}"),
        ):
            resolved = _bytes_from_row(row)
            if resolved is not None:
                return resolved[0], resolved[1], f"{prefix}:{(row or {}).get('id')}"
    return None


async def _character_image(character_id: str | None, profile: Mapping[str, Any]) -> tuple[bytes, str, str] | None:
    """The per-character reference, falling back to the card avatar -- a genuine
    likeness, always present, so this source works before anything is uploaded."""
    if not character_id:
        return None
    payload, mime = profile.get("reference_image_b64"), profile.get("reference_mime")
    if isinstance(payload, str) and payload and isinstance(mime, str) and mime:
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            data = b""
        if data:
            return data, mime, f"character:{character_id}"
    avatar = await get_character_avatar(character_id)
    if avatar and avatar[0]:
        return avatar[0], avatar[1] or "image/png", f"character:{character_id}"
    return None


def _constraints(entry: Mapping[str, Any]) -> dict:
    """Per-slot mime/size policy, as the slot's own record states it.

    Data-driven rather than a parameter threaded down from the hook: a ComfyUI
    graph slot declares neither and gets the shared defaults, while the cloud
    adapter's synthetic slot carries the provider's accepted mimes and the tighter
    base64-in-JSON byte cap. Nothing above here has to know which backend is active.
    """
    limits: dict[str, Any] = {}
    mimes = entry.get("mimes")
    if isinstance(mimes, (list, tuple)) and mimes:
        limits["allowed"] = tuple(str(m) for m in mimes)
    max_bytes = entry.get("max_bytes")
    if isinstance(max_bytes, int) and not isinstance(max_bytes, bool) and max_bytes > 0:
        limits["max_bytes"] = max_bytes
    return limits


async def _resolved(slot: Any, source: str, data: bytes, mime: str, origin: str, **limits: Any) -> ResolvedReference:
    # Bounding is PIL work, so it goes off-thread like the display re-encode does.
    data, mime = await asyncio.to_thread(normalize_reference, data, mime, **limits)
    return ResolvedReference(
        slot=(str(slot[0]), str(slot[1])),
        source=source,
        data=data,
        mime=mime,
        origin=origin,
        digest=hashlib.sha256(data).hexdigest(),
    )


def _unresolved(label: str, source: str) -> ImageGenerationError:
    tried = " or ".join(SOURCE_LABELS[name] for name in REFERENCE_SOURCES.get(source, ())) or "any configured source"
    return ImageGenerationError(
        f"This workflow needs a reference image for {label}, but {tried} is not available. "
        "Generate or upload an image in this chat first, or set a character reference image in settings."
    )


async def resolve_references(
    entries: Sequence[Mapping[str, Any]],
    *,
    history: Sequence[Mapping[str, Any]],
    anchor_id: int,
    character_id: str | None,
    profile: Mapping[str, Any] | None = None,
) -> tuple[ResolvedReference, ...]:
    """Bytes for every mapped `LoadImage` slot, for a fresh render.

    An unresolvable slot is a hard failure with a specific message rather than a
    silent substitution: a graph built around a reference produces nonsense
    without one.
    """
    if not entries:
        return ()
    normalized_profile = normalize_profile(profile)
    # Each source is resolved at most once per render even when several slots
    # share it, so a two-slot graph reads the branch once.
    cache: dict[str, tuple[bytes, str, str] | None] = {}
    resolved: list[ResolvedReference] = []
    for entry in entries:
        slot = entry.get("slot")
        source = str(entry.get("source") or "")
        found: tuple[bytes, str, str] | None = None
        for name in REFERENCE_SOURCES.get(source, ()):
            if name not in cache:
                cache[name] = (
                    _previous_image(history, anchor_id)
                    if name == "previous"
                    else await _character_image(character_id, normalized_profile)
                )
            found = cache[name]
            if found is not None:
                break
        if found is None:
            raise _unresolved(str(entry.get("label") or (slot[0] if slot else "this workflow")), source)
        resolved.append(await _resolved(slot, source, *found, **_constraints(entry)))
    return tuple(resolved)


async def _origin_bytes(origin: str) -> tuple[bytes, str] | None:
    kind, _, ident = origin.partition(":")
    if kind == "attachment" and ident.isdigit():
        return _bytes_from_row(await get_workflow_attachment_by_id(int(ident)))
    if kind == "upload":
        # "upload:<message id>:<attachment id>" -- user attachments are only
        # readable per-message, so the origin has to carry both.
        message_id, _, attachment_id = ident.partition(":")
        if message_id.isdigit() and attachment_id.isdigit():
            for row in await get_user_attachments_for_message(int(message_id)):
                if row.get("id") == int(attachment_id):
                    return _bytes_from_row(row)
        return None
    if kind == "character" and ident:
        # The card's *current* image, not a snapshot: this origin addresses a
        # setting rather than a chat message, so changing it and rerolling applies.
        current = await _character_image(ident, normalize_profile(await get_workflow_character_state(ident, WORKFLOW_ID)))
        return (current[0], current[1]) if current else None
    return None


async def refetch_references(
    recorded: Any,
    *,
    slots: Sequence[Mapping[str, Any]] = (),
) -> tuple[ResolvedReference, ...]:
    """Re-fetch a stored render's references strictly by recorded origin.

    Reroll promises that only the seed changes, so this never re-resolves: a
    deleted or byte-evicted origin fails loudly instead of silently changing the
    subject. The origin carries everything needed, which is what lets this run on
    the history-free reroll ctx.

    `slots` is the *target's* slot list, not the record's: the stored shape is
    deliberately just `{slot, source, origin, digest}`, so the mime/size policy has
    to come from what will render this time. Matched by slot, so a re-render on a
    provider that takes PNG/JPEG converts even though the record says nothing.
    """
    if not isinstance(recorded, (list, tuple)) or not recorded:
        return ()
    policy = {tuple(str(part) for part in slot["slot"]): _constraints(slot) for slot in slots if slot.get("slot")}
    resolved: list[ResolvedReference] = []
    for entry in recorded:
        if not isinstance(entry, Mapping):
            continue
        slot = entry.get("slot")
        origin = str(entry.get("origin") or "")
        if not isinstance(slot, (list, tuple)) or len(slot) != 2 or not origin:
            raise ImageGenerationError("This image's reference is no longer recorded; regenerate it instead of rerolling")
        found = await _origin_bytes(origin)
        if found is None:
            raise ImageGenerationError(
                "The reference image this render used is gone, so it cannot be reproduced exactly. "
                "Regenerate the image instead of rerolling it."
            )
        limits = policy.get(tuple(str(part) for part in slot), {})
        resolved.append(await _resolved(slot, str(entry.get("source") or ""), found[0], found[1], origin, **limits))
    return tuple(resolved)
