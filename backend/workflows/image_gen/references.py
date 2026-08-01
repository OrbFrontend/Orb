"""Resolve a workflow's mapped `LoadImage` slots to actual reference bytes.

Sits at the `image_gen` top level rather than under `engine/`: this reads
conversation state through the workflow toolkit, while `engine/` stays
ComfyUI-only. The engine consumes the resolved bytes -- uploading and patching
are its half of the split.

Two entry points, because the two replay routes have genuinely different
promises. `resolve_references` picks the reference a *fresh* render should use
from the branch as it stands. `refetch_references` re-fetches strictly by the
origin a stored render recorded, so a reroll changes only the seed.
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
from .engine.graph import reference_slots

# How each source reads in an error message and in Render details. The names are
# the ones the import picker uses, not the internal keys.
SOURCE_LABELS = {
    "previous": "the previous image in the chat",
    "character": "the character reference image",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bytes_from_row(row: Mapping[str, Any]) -> tuple[bytes, str] | None:
    """Decoded image bytes off an attachment row, or None when unusable.

    An evicted row holds the sentinel rather than base64, and a row whose bytes
    do not decode is not a reference -- both fall through to the next candidate
    rather than raising, so one bad row cannot block a walk-back that has other
    images to offer.
    """
    payload = row.get("data_b64")
    mime = row.get("mime_type")
    if not isinstance(payload, str) or not payload or payload == EVICTED_MARKER:
        return None
    if not isinstance(mime, str) or not mime.startswith("image/"):
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError):
        return None
    return (data, mime) if data else None


def _active_generated_image(message: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The image_gen attachment this message is actually *showing*.

    Reroll siblings share a group root; the root's `active_sibling_id` names the
    one on screen (NULL means the newest). Same rule the chat renderer uses, so a
    reference is the image the user is looking at rather than the first variant
    that happened to be generated.
    """
    attachments = message.get("workflow_attachments")
    if not isinstance(attachments, (list, tuple)):
        return None
    ours = [a for a in attachments if isinstance(a, Mapping) and a.get("workflow_id") == WORKFLOW_ID]
    if not ours:
        return None
    by_id = {a.get("id"): a for a in ours}
    groups: dict[Any, list[Mapping[str, Any]]] = {}
    for attachment in ours:
        parent = attachment.get("parent_attachment_id")
        root_id = parent if parent is not None and parent in by_id else attachment.get("id")
        groups.setdefault(root_id, []).append(attachment)
    # Latest group first: the most recent image on the message is the one "the
    # previous image" means.
    root_id = max(groups, key=lambda key: (key is not None, key))
    siblings = sorted(groups[root_id], key=lambda a: a.get("id") or 0)
    active_id = (by_id.get(root_id) or {}).get("active_sibling_id")
    if active_id is not None:
        for sibling in siblings:
            if sibling.get("id") == active_id:
                return sibling
    return siblings[-1]


def _uploaded_image(message: Mapping[str, Any]) -> Mapping[str, Any] | None:
    attachments = message.get("user_attachments")
    for attachment in reversed(attachments if isinstance(attachments, (list, tuple)) else []):
        if isinstance(attachment, Mapping) and str(attachment.get("mime_type") or "").startswith("image/"):
            return attachment
    return None


def _previous_image(history: Sequence[Mapping[str, Any]], anchor_id: int) -> tuple[bytes, str, str] | None:
    """The newest usable image on this branch before the anchor.

    The anchor itself is excluded: a regenerate would otherwise feed the image
    already attached to the message back in as its own reference, so every reroll
    would edit the previous render instead of the scene. `history` is already
    sliced through the anchor by the caller, so "before" is just "earlier in this
    list".

    A user's own upload counts as a previous image -- "edit this photo of me" is
    the same request as "edit that render", and refusing it would send the walk
    past the very image the user just attached. The `upload:` origin carries the
    message id too, since user attachments are only readable per-message.
    """
    for message in reversed(list(history)):
        if message.get("id") == anchor_id:
            continue
        generated = _active_generated_image(message)
        if generated is not None:
            resolved = _bytes_from_row(generated)
            if resolved is not None:
                return resolved[0], resolved[1], f"attachment:{generated.get('id')}"
        uploaded = _uploaded_image(message)
        if uploaded is not None:
            resolved = _bytes_from_row(uploaded)
            if resolved is not None:
                return resolved[0], resolved[1], f"upload:{message.get('id')}:{uploaded.get('id')}"
    return None


async def _character_image(character_id: str | None, profile: Mapping[str, Any]) -> tuple[bytes, str, str] | None:
    """The per-character reference, falling back to the card avatar.

    The avatar is a genuine likeness of the character and is always present on an
    imported card, so it makes the character source useful before the user has
    uploaded anything dedicated.
    """
    if not character_id:
        return None
    payload = profile.get("reference_image_b64")
    mime = profile.get("reference_mime")
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


def _sources(source: str) -> tuple[str, ...]:
    return REFERENCE_SOURCES.get(source, ())


def _unresolved(label: str, source: str) -> ImageGenerationError:
    tried = " or ".join(SOURCE_LABELS[name] for name in _sources(source)) or "any configured source"
    return ImageGenerationError(
        f"This workflow needs a reference image for {label}, but {tried} is not available. "
        "Generate or upload an image in this chat first, or set a character reference image in settings."
    )


async def resolve_references(
    slots: Mapping[str, Any],
    *,
    history: Sequence[Mapping[str, Any]],
    anchor_id: int,
    character_id: str | None,
    profile: Mapping[str, Any] | None = None,
) -> tuple[ResolvedReference, ...]:
    """Bytes for every mapped `LoadImage` slot, for a fresh render.

    An unresolvable slot is a hard failure with a specific message rather than a
    silent substitution: a graph built around a reference produces nonsense
    without one, and "clear failure over silent substitution" is the stance the
    rest of this workflow already takes.
    """
    entries = reference_slots(slots)
    if not entries:
        return ()
    normalized_profile = normalize_profile(profile)
    # Each source is resolved at most once per render even when several slots
    # share it, so a two-slot graph reads the branch once.
    found_by_source: dict[str, tuple[bytes, str, str] | None] = {}
    resolved: list[ResolvedReference] = []
    for entry in entries:
        slot = entry.get("slot")
        source = str(entry.get("source") or "")
        label = str(entry.get("label") or (slot[0] if slot else "this workflow"))
        found: tuple[bytes, str, str] | None = None
        for name in _sources(source):
            if name not in found_by_source:
                found_by_source[name] = (
                    _previous_image(history, anchor_id)
                    if name == "previous"
                    else await _character_image(character_id, normalized_profile)
                )
            found = found_by_source[name]
            if found is not None:
                break
        if found is None:
            raise _unresolved(label, source)
        data, mime, origin = found
        resolved.append(await _resolved(slot, source, data, mime, origin))
    return tuple(resolved)


async def _resolved(slot: Any, source: str, data: bytes, mime: str, origin: str) -> ResolvedReference:
    # Bounding is PIL work, so it goes off-thread like the display re-encode does.
    data, mime = await asyncio.to_thread(normalize_reference, data, mime)
    return ResolvedReference(
        slot=(str(slot[0]), str(slot[1])),
        source=source,
        data=data,
        mime=mime,
        origin=origin,
        digest=_digest(data),
    )


async def _origin_bytes(origin: str) -> tuple[bytes, str] | None:
    kind, _, ident = origin.partition(":")
    if kind == "attachment" and ident.isdigit():
        row = await get_workflow_attachment_by_id(int(ident))
        return _bytes_from_row(row) if row else None
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
        # The card's profile, not a snapshot: the origin addresses a setting, and
        # re-reading it is what makes "change the character reference, then reroll"
        # do what it says.
        profile = normalize_profile(await get_workflow_character_state(ident, WORKFLOW_ID))
        current = await _character_image(ident, profile)
        return (current[0], current[1]) if current else None
    return None


async def refetch_references(recorded: Any) -> tuple[ResolvedReference, ...]:
    """Re-fetch a stored render's references strictly by recorded origin.

    Reroll promises that only the seed changes, so this never re-resolves: a
    deleted or byte-evicted origin fails loudly instead of substituting a
    different image that would silently change the subject of the picture. The
    origin carries everything needed, which is what lets this run on the reroll
    ctx -- deliberately history-free.

    A `character:` origin re-reads that card's *current* reference image rather
    than a snapshot, because it addresses a setting rather than a chat message:
    changing the character reference and rerolling is a coherent request, where
    "reroll onto whatever image the branch has now" is not.
    """
    if not isinstance(recorded, (list, tuple)) or not recorded:
        return ()
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
        resolved.append(await _resolved(slot, str(entry.get("source") or ""), found[0], found[1], origin))
    return tuple(resolved)
