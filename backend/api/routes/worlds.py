"""Worlds, lorebook-entry CRUD, import/export, and the Dynamic Worlds changeset routes."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ...database import (
    OverlayStateConflict,
    RevisionConflict,
    count_pending_changesets,
    create_lorebook_entry,
    create_world,
    delete_lorebook_entry,
    delete_world,
    disable_character_linked_worlds,
    get_active_lorebook_entries,
    get_lorebook_entries,
    get_world,
    get_world_changesets,
    get_worlds,
    import_lorebook_entries,
    update_lorebook_entry,
    update_world,
    update_world_changeset,
)
from ...features import lorebook
from ...pipeline.world_proposal import reevaluate_changeset
from ..deps import (
    _normalise_lorebook_entry,
    lorebook_to_book,
    require_changeset,
    require_lorebook_entry,
    require_world,
)
from ..schemas import (
    ChangesetApply,
    ChangesetEdit,
    LorebookEntryCreate,
    LorebookEntryUpdate,
    LorebookImportPayload,
    WorldCreate,
    WorldDynamicToggle,
    WorldUpdate,
)

router = APIRouter()


# Worlds ──


@router.get("/api/worlds")
async def api_list_worlds():
    """Every World, each carrying its pending-proposal count.

    The counts come from one grouped query rather than one per World, so the
    sidebar badge costs the same whether the user has two lorebooks or fifty.
    """
    worlds = await get_worlds()
    pending = await count_pending_changesets([w["id"] for w in worlds])
    return [{**w, "pending_changesets": pending.get(w["id"], 0)} for w in worlds]


@router.post("/api/worlds")
async def api_create_world(data: WorldCreate):
    return await create_world(data.model_dump())


@router.post("/api/worlds/deactivate-linked")
async def api_deactivate_linked_worlds():
    """Turn off every enabled World a character card links to. The client's boot sweep.

    A linked World is on loan to the character in play; a fresh page has nobody
    in play, so carrying one over from the last session would leak that
    character's lore into whatever chat is opened next. Floating Worlds are
    global lore and keep whatever state the user left them in.
    """
    return {"disabled": await disable_character_linked_worlds()}


@router.put("/api/worlds/{world_id}")
async def api_update_world(world_id: str, data: WorldUpdate):
    result = await update_world(world_id, data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(status_code=404, detail="World not found")
    return result


@router.put("/api/worlds/{world_id}/dynamic")
async def api_set_dynamic_enabled(
    data: WorldDynamicToggle,
    world: dict = Depends(require_world),  # noqa: B008
):
    """Turn Dynamic Worlds on or off for one World.

    A dedicated route rather than a field on the general update so the intent is
    explicit in the audit and in the client. Turning it off stops new proposals
    but changes nothing already applied — accepted lore is ordinary lore.
    """
    return await update_world(world["id"], {"dynamic_enabled": data.enabled})


@router.delete("/api/worlds/{world_id}")
async def api_delete_world(world_id: str):
    if not await delete_world(world_id):
        raise HTTPException(status_code=404, detail="World not found")
    return {"ok": True}


# Lorebook Entries ──


@router.get("/api/worlds/{world_id}/entries")
async def api_list_lorebook_entries(
    view: Literal["all", "authored", "effective"] = "all",
    world: dict = Depends(require_world),  # noqa: B008
):
    """List a World's entries.

    ``all`` (the default, and what the drawer uses) returns both layers
    including archived overlay rows, so the UI can label them and show history.
    ``effective`` is the projection the prompt actually sees; ``authored`` is the
    user-owned layer alone.
    """
    entries = await get_lorebook_entries(world["id"])
    if view == "authored":
        return [e for e in entries if e.get("entry_layer") != "dynamic"]
    if view == "effective":
        return lorebook.select_effective_entries(entries)
    return entries


@router.post("/api/worlds/{world_id}/entries")
async def api_create_lorebook_entry(
    data: LorebookEntryCreate,
    world: dict = Depends(require_world),  # noqa: B008
):
    return await create_lorebook_entry(world["id"], data.model_dump())


@router.get("/api/worlds/{world_id}/entries/{entry_id}")
async def api_get_lorebook_entry(
    entry: dict = Depends(require_lorebook_entry),  # noqa: B008
):
    return entry


@router.put("/api/worlds/{world_id}/entries/{entry_id}")
async def api_update_lorebook_entry(
    data: LorebookEntryUpdate,
    entry: dict = Depends(require_lorebook_entry),  # noqa: B008
):
    result = await update_lorebook_entry(entry["id"], data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(status_code=404, detail="Entry not found")
    return result


@router.delete("/api/worlds/{world_id}/entries/{entry_id}")
async def api_delete_lorebook_entry(
    entry: dict = Depends(require_lorebook_entry),  # noqa: B008
):
    if not await delete_lorebook_entry(entry["id"]):
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"ok": True}


@router.post("/api/worlds/{world_id}/import")
async def api_import_lorebook(world_id: str, payload: LorebookImportPayload):
    world = await get_world(world_id)
    if not world:
        raise HTTPException(status_code=404, detail="World not found")

    raw_entries = payload.entries
    # Normalise both formats into a flat list of dicts
    if isinstance(raw_entries, dict):
        # Standalone World Info export: {"0": {...}, "1": {...}}
        items = list(raw_entries.values())
    elif isinstance(raw_entries, list):
        # Tavern V2 character_book: [...]
        items = raw_entries
    else:
        raise HTTPException(status_code=422, detail="entries must be an object or array")

    normalised = [_normalise_lorebook_entry(item) for item in items if isinstance(item, dict)]
    # One transaction, one revision bump: a half-imported World must never be
    # visible, and one user action must not invalidate pending proposals N times.
    created = await import_lorebook_entries(world_id, normalised)

    return {"imported": len(created), "entries": created}


@router.get("/api/worlds/{world_id}/export")
async def api_export_lorebook(world_id: str, view: Literal["authored", "effective"] = "authored"):
    """Export a lorebook as a standalone Tavern V2 ``character_book`` JSON file.

    ``authored`` is the default and the backward-compatible behaviour: the file
    holds the user's own lore, unchanged by whatever the Agent has proposed or
    the user has accepted since. ``effective`` is opt-in and exports the
    projection instead — the World as the prompt currently sees it, replacements
    applied and suppressions removed.
    """
    world = await get_world(world_id)
    if not world:
        raise HTTPException(status_code=404, detail="World not found")

    entries = await get_lorebook_entries(world_id)
    if view == "effective":
        exported: list[Any] = list(lorebook.select_effective_entries(entries))
    else:
        exported = [e for e in entries if e.get("entry_layer") != "dynamic"]
    book = lorebook_to_book(world["name"], exported)
    safe_name = "".join(c for c in world["name"] if c.isalnum() or c in " _-").strip() or "lorebook"
    suffix = "" if view == "authored" else f" ({view})"
    return Response(
        content=json.dumps(book, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}{suffix}.json"'},
    )


@router.get("/api/lorebook-entries/active")
async def api_get_active_lorebook_entries():
    """The enabled entries of enabled Worlds, projected to what the prompt sees."""
    return lorebook.select_effective_entries(await get_active_lorebook_entries())


# Dynamic Worlds — changesets ──


@router.get("/api/worlds/{world_id}/changesets")
async def api_list_changesets(
    status: Literal["all", "pending", "history"] = "all",
    world: dict = Depends(require_world),  # noqa: B008
):
    """Changesets for a World, newest first.

    ``pending`` is the review queue (including the stale ones, which the user
    must still see and act on); ``history`` is everything already decided.
    """
    if status == "pending":
        statuses: tuple[str, ...] | None = ("pending", "stale")
    elif status == "history":
        statuses = ("applied", "rejected", "superseded", "reverted")
    else:
        statuses = None
    return await get_world_changesets(world["id"], statuses=list(statuses) if statuses else None)


def _open_guard(changeset: dict) -> None:
    """Allow actions shared by pending and stale review items."""
    if changeset["status"] not in ("pending", "stale"):
        raise HTTPException(status_code=409, detail=f"This proposal is already {changeset['status']}.")


def _pending_guard(changeset: dict) -> None:
    """Only a live proposal may be edited or applied; stale means re-evaluate."""
    if changeset["status"] != "pending":
        detail = (
            "This proposal is stale. Re-evaluate it against the current world before applying."
            if changeset["status"] == "stale"
            else f"This proposal is already {changeset['status']}."
        )
        raise HTTPException(status_code=409, detail=detail)


@router.put("/api/worlds/{world_id}/changesets/{changeset_id}")
async def api_edit_changeset(
    data: ChangesetEdit,
    changeset: dict = Depends(require_changeset),  # noqa: B008
):
    """Edit a pending proposal in place without deciding on it."""
    _pending_guard(changeset)
    patch: dict[str, Any] = {}
    if data.summary is not None:
        patch["summary"] = data.summary
    if data.operations is not None:
        patch["operations"] = [op.model_dump() for op in data.operations]
    if not patch:
        return changeset
    try:
        return await update_world_changeset(int(changeset["id"]), patch, expected_statuses=("pending",))
    except OverlayStateConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/api/worlds/{world_id}/changesets/{changeset_id}/apply")
async def api_apply_changeset(
    data: ChangesetApply,
    changeset: dict = Depends(require_changeset),  # noqa: B008
):
    """Apply a pending proposal atomically, optionally edited in the same request.

    ``409`` on a revision conflict: the World changed since this was proposed, so
    the proposal is marked stale and the client is told to Re-evaluate rather
    than offered a force-apply. Nothing is applied on that path.
    """
    _pending_guard(changeset)
    operations = [op.model_dump() for op in data.operations] if data.operations is not None else None
    try:
        return await lorebook.accept_changeset(changeset, operations=operations, summary=data.summary)
    except RevisionConflict as e:
        # The revision check and this bookkeeping update are deliberately
        # separate: the failed apply rolled back without changing the World.
        # A concurrent reject may have decided the proposal in between; that is
        # still a clean conflict response, not a reason to turn this request
        # into a 500 while trying to overwrite the winning decision.
        try:
            await lorebook.mark_stale(int(changeset["id"]))
        except OverlayStateConflict:
            pass
        raise HTTPException(
            status_code=409,
            detail=(
                "This world changed since the proposal was made (now at revision "
                f"{e.actual}, proposed against {e.expected}). Re-evaluate it against the current world."
            ),
        ) from e
    except OverlayStateConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/api/worlds/{world_id}/changesets/{changeset_id}/reject")
async def api_reject_changeset(
    changeset: dict = Depends(require_changeset),  # noqa: B008
):
    _open_guard(changeset)
    try:
        return await lorebook.reject_changeset(changeset)
    except OverlayStateConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/api/worlds/{world_id}/changesets/{changeset_id}/re-evaluate")
async def api_reevaluate_changeset(
    changeset: dict = Depends(require_changeset),  # noqa: B008
):
    """Derive a fresh proposal from this one's source messages and the current World.

    The stale original is retired either way. A ``null`` changeset in the
    response means the model, looking at the World as it now stands, found
    nothing left to propose — which is a legitimate answer, not a failure.
    """
    _open_guard(changeset)
    try:
        replacement = await reevaluate_changeset(changeset)
    except OverlayStateConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"changeset": replacement}


@router.post("/api/worlds/{world_id}/changesets/{changeset_id}/undo")
async def api_undo_changeset(
    changeset: dict = Depends(require_changeset),  # noqa: B008
):
    """Reverse an applied changeset by applying its compensating changeset."""
    if changeset["status"] != "applied":
        raise HTTPException(status_code=409, detail="Only an applied change can be undone.")
    try:
        return await lorebook.undo_changeset(changeset)
    except RevisionConflict as e:
        raise HTTPException(
            status_code=409,
            detail=f"This world moved to revision {e.actual} while the undo was being prepared. Try again.",
        ) from e
    except OverlayStateConflict as e:
        raise HTTPException(
            status_code=409,
            detail=f"{e} Review the world's dynamic entries before undoing.",
        ) from e


@router.post("/api/worlds/{world_id}/reset")
async def api_reset_world(world: dict = Depends(require_world)):  # noqa: B008
    """Archive every dynamic entry, restoring the authored World exactly.

    Itself an undoable changeset — the overlay is retired, never deleted, so the
    reset can be reversed like any other change.
    """
    try:
        changeset = await lorebook.reset_world_to_authored(world["id"])
    except (OverlayStateConflict, RevisionConflict) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"changeset": changeset, "reset": changeset is not None}
