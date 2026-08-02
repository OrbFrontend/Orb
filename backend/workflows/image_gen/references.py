"""Resolve a workflow's mapped `LoadImage` slots to actual reference bytes.

Sits at the `image_gen` top level rather than under `engine/`: this reads
conversation state through the workflow toolkit, while `engine/` stays
ComfyUI-only. Uploading and patching are the engine's half of the split.

Two entry points, because the two render routes make different promises.
`resolve_references` picks what a *fresh* render should use from the branch as it
stands; `refetch_references` re-fetches strictly by recorded origin, so a reroll
changes only the seed.

Both answer against a *slot list the target supplied*, and every slot carries its
own policy: which mimes it accepts, how many bytes it will take, and whether it
can render at all without one. Nothing here knows which backend is active.
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
from .config import REFERENCE_MIMES, REFERENCE_SOURCES, WORKFLOW_ID, normalize_profile
from .engine import ImageGenerationError
from .engine.contracts import ResolvedReference
from .engine.display_encode import normalize_reference

# How each source reads in an error message. The names the import picker uses.
SOURCE_LABELS = {
    "previous": "the previous image in the chat",
    "character": "the character reference image",
}

# How far back a `previous` slot looks, in messages on this branch.
#
# Unbounded, the first render in a conversation permanently retires the character
# reference: `previous_or_character` would find *something* forever after, and a
# picture from two hundred messages ago -- a different scene, often a different
# character -- would outrank a likeness the user set on purpose. A bound is what
# makes the fallback half of that source reachable again.
PREVIOUS_LOOKBACK_MESSAGES = 30


def _bytes_from_row(row: Mapping[str, Any] | None) -> tuple[bytes, str] | None:
    """Decoded image bytes off an attachment row, or None when unusable.

    An evicted row holds the sentinel rather than base64; both that and a row
    that does not decode fall through to the next candidate rather than raising,
    so one bad row cannot block a walk-back with images left to try.

    The mime must be one Orb declares it accepts, not merely ``image/*``. A user
    can upload a HEIC, an AVIF or an SVG, and any of the three reaches an image
    backend as bytes it cannot decode inside a body labelled as something else --
    so an unsupported upload is skipped here and the walk-back continues, exactly
    as it does for an evicted row. `REFERENCE_MIMES` is the one list; the
    per-character reference already answered to it.
    """
    payload = (row or {}).get("data_b64")
    mime = (row or {}).get("mime_type")
    if not isinstance(payload, str) or not payload or payload == EVICTED_MARKER:
        return None
    if not isinstance(mime, str) or mime.lower() not in REFERENCE_MIMES:
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError):
        return None
    return (data, mime.lower()) if data else None


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
    """The newest upload on this message that Orb accepts as a reference.

    Filtered on the mime alone rather than by calling `_bytes_from_row`, which
    would decode a multi-megabyte payload here and again at the point of use.
    """
    uploads = [a for a in _rows(message, "user_attachments") if str(a.get("mime_type") or "").lower() in REFERENCE_MIMES]
    return uploads[-1] if uploads else None


def _previous_image(history: Sequence[Mapping[str, Any]], anchor_id: int) -> tuple[bytes, str, str] | None:
    """The newest usable image on this branch before the anchor.

    The anchor is excluded: a regenerate would otherwise feed the image already
    attached to the message back in as its own reference, editing the previous
    render instead of the scene. A user's own upload counts -- "edit this photo
    of me" is the same request -- and its origin carries the message id too,
    since user attachments are only readable per-message.

    Bounded by `PREVIOUS_LOOKBACK_MESSAGES`; see that constant.
    """
    scanned = 0
    for message in reversed(list(history)):
        if message.get("id") == anchor_id:
            continue
        scanned += 1
        if scanned > PREVIOUS_LOOKBACK_MESSAGES:
            return None
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
    try:
        avatar = await get_character_avatar(character_id)
    except (ValueError, TypeError):
        # A card whose stored avatar is not decodable base64 is a missing source,
        # not a failed render: the walk-back has other candidates to try.
        return None
    if avatar and avatar[0]:
        return avatar[0], avatar[1] or "image/png", f"character:{character_id}"
    return None


def _constraints(entry: Mapping[str, Any]) -> dict:
    """Per-slot mime/size policy, as the slot's own record states it.

    Data-driven rather than a parameter threaded down from the hook: each adapter
    states what its slots accept -- ComfyUI the three mimes it can name a file
    with, the cloud adapter the provider's accepted mimes and the tighter
    base64-in-JSON byte cap. Nothing above here has to know which backend is
    active, and a slot that states nothing gets the shared defaults.
    """
    limits: dict[str, Any] = {}
    mimes = entry.get("mimes")
    if isinstance(mimes, (list, tuple)) and mimes:
        limits["allowed"] = tuple(str(m) for m in mimes)
    max_bytes = entry.get("max_bytes")
    if isinstance(max_bytes, int) and not isinstance(max_bytes, bool) and max_bytes > 0:
        limits["max_bytes"] = max_bytes
    return limits


def _required(entry: Mapping[str, Any]) -> bool:
    """Whether this slot's render is impossible without an image.

    A ComfyUI graph built around a `LoadImage` is: rendering it unfilled submits
    the exporter's stale filename and draws whatever that pointed at. A cloud
    provider's synthetic slot is not -- the same model has a plain generations
    endpoint one field away -- so an unresolvable one degrades with a note
    instead of refusing. Declared by the adapter that owns the slot, because
    "can this render without a reference" is a fact about the backend.
    """
    return entry.get("required") is not False


def _slot_key(slot: Any) -> tuple[str, ...] | None:
    if not isinstance(slot, (list, tuple)) or len(slot) != 2:
        return None
    return tuple(str(part) for part in slot)


async def _resolved(
    slot: Any,
    source: str,
    data: bytes,
    mime: str,
    origin: str,
    **limits: Any,
) -> ResolvedReference:
    # Bounding is PIL work, so it goes off-thread like the display re-encode does.
    source_digest = hashlib.sha256(data).hexdigest()
    data, mime = await asyncio.to_thread(normalize_reference, data, mime, **limits)
    return ResolvedReference(
        slot=(str(slot[0]), str(slot[1])),
        source=source,
        data=data,
        mime=mime,
        origin=origin,
        digest=hashlib.sha256(data).hexdigest(),
        source_digest=source_digest,
    )


def _unresolved(label: str, source: str) -> ImageGenerationError:
    names = REFERENCE_SOURCES.get(source, ())
    tried = " or ".join(SOURCE_LABELS.get(name, name) for name in names) or "any configured source"
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
    """Bytes for every mapped reference slot, for a fresh render.

    An unresolvable *required* slot is a hard failure with a specific message
    rather than a silent substitution: a graph built around a reference produces
    nonsense without one. An unresolvable optional slot is simply absent from the
    result, and the caller discloses that on the attachment -- see `_required`.
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
            if _required(entry):
                raise _unresolved(str(entry.get("label") or (slot[0] if slot else "this workflow")), source)
            continue
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


def _pair_with_slots(
    recorded: Sequence[Mapping[str, Any]],
    slots: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any] | None]]:
    """Which of *this* render's slots carries each recorded reference.

    Matched by slot id first, so an unchanged target replays byte-identically.
    What is left over is matched **positionally**, which is what makes replay work
    across backends at all: a cloud reference is recorded against the synthetic
    ``("cloud", "image_0")`` and a ComfyUI one against a node id in whatever graph
    ran, so the two never match by key. Without the positional pass a ComfyUI
    image rerolled under cloud sent its reference with ComfyUI's mime policy (the
    provider was told PNG and handed a WebP), and the same reroll in the other
    direction patched a graph node named ``cloud`` that does not exist.

    A recorded reference with no slot left pairs with ``None``; the caller drops
    it and discloses that rather than submitting it nowhere.
    """
    # Tracked by index, never by value: two slots on one target can be equal
    # dicts, and removing "the equal one" would consume the wrong slot.
    by_key: dict[tuple[str, ...], int] = {}
    for index, slot in enumerate(slots):
        key = _slot_key(slot.get("slot"))
        if key is not None and key not in by_key:
            by_key[key] = index
    taken: set[int] = set()
    matched: list[int | None] = []
    for entry in recorded:
        key = _slot_key(entry.get("slot"))
        index = by_key.get(key) if key is not None else None
        if index is not None and index not in taken:
            taken.add(index)
            matched.append(index)
        else:
            matched.append(None)
    free = (index for index in range(len(slots)) if index not in taken)
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]] = []
    for entry, index in zip(recorded, matched, strict=True):
        if index is None:
            index = next(free, None)
        pairs.append((entry, slots[index] if index is not None else None))
    return pairs


async def refetch_references(
    recorded: Any,
    *,
    slots: Sequence[Mapping[str, Any]] = (),
) -> tuple[ResolvedReference, ...]:
    """Re-fetch a stored render's references strictly by recorded origin.

    Reroll promises that only the seed changes, so this never re-resolves: a
    deleted or byte-evicted origin fails loudly instead of silently changing the
    subject, and so does an origin whose *content* changed underneath its id --
    rehydrating an attachment on a seedless provider rewrites the same row with a
    different picture. The origin carries everything needed, which is what lets
    this run on the history-free reroll ctx.

    `slots` is the *target's* slot list, not the record's: the stored shape is
    deliberately just ``{slot, source, origin, digest, source_digest}``, so the
    mime/size policy and the slot each reference will actually occupy both have
    to come from what will render this time. See `_pair_with_slots`. Passing no
    slots means "no target to speak of" -- the record is echoed back unbounded,
    which only a caller with nothing to render against should do.
    """
    entries = [entry for entry in recorded if isinstance(entry, Mapping)] if isinstance(recorded, (list, tuple)) else []
    if not entries:
        _require_all_filled(slots, ())
        return ()
    resolved: list[ResolvedReference] = []
    for entry, target in _pair_with_slots(entries, slots):
        slot = target.get("slot") if target is not None else entry.get("slot")
        origin = str(entry.get("origin") or "")
        if _slot_key(entry.get("slot")) is None or not origin:
            raise ImageGenerationError("This image's reference is no longer recorded; regenerate it instead of rerolling")
        if slots and target is None:
            # Nowhere to put it on this render. The caller discloses the drop.
            continue
        found = await _origin_bytes(origin)
        if found is None:
            raise ImageGenerationError(
                "The reference image this render used is gone, so it cannot be reproduced exactly. "
                "Regenerate the image instead of rerolling it."
            )
        limits = _constraints(target) if target is not None else {}
        reference = await _resolved(slot, str(entry.get("source") or ""), found[0], found[1], origin, **limits)
        _verify_unchanged(entry, reference)
        resolved.append(reference)
    _require_all_filled(slots, resolved)
    return tuple(resolved)


def _require_all_filled(slots: Sequence[Mapping[str, Any]], resolved: Sequence[ResolvedReference]) -> None:
    """Refuse a replay that leaves a slot the render cannot run without.

    This is the whole of what a style change on reroll used to be refused for.
    The old rule refused whenever the new style took references at all, on the
    grounds that the recorded slots named node ids in the *old* graph -- true,
    but the origins never did, and re-keying them is what makes the common case
    (one identity image, one `LoadImage`, a different style) simply work. What
    genuinely cannot be replayed is a target that needs more images than the
    record holds, which is this.
    """
    filled = {_slot_key(reference.slot) for reference in resolved}
    missing = [slot for slot in slots if _required(slot) and _slot_key(slot.get("slot")) not in filled]
    if not missing:
        return
    labels = ", ".join(str(slot.get("label") or "reference image") for slot in missing)
    raise ImageGenerationError(
        f"This style needs a reference image the stored image did not record ({labels}), so it cannot be "
        "reproduced by a reroll. Regenerate the image under this style instead."
    )


def _verify_unchanged(entry: Mapping[str, Any], reference: ResolvedReference) -> None:
    """Refuse a replay whose origin now holds different bytes than it recorded.

    Compared on `source_digest`, which is taken before the destination's policy
    touches the bytes, so a reference re-keyed onto another backend's slot is not
    accused of changing merely because it converted differently this time.

    A `character:` origin is exempt by design: it addresses a *setting*, and
    "change the character reference, then reroll" is documented to apply. Records
    written before this field existed carry no digest and are not second-guessed.
    """
    if reference.origin.startswith("character:"):
        return
    recorded = entry.get("source_digest")
    if not isinstance(recorded, str) or not recorded or recorded == reference.source_digest:
        return
    raise ImageGenerationError(
        "The reference image this render used has been replaced since, so the image cannot be reproduced "
        "exactly. Regenerate the image instead of rerolling it."
    )
