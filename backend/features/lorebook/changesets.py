"""Dynamic Worlds: the changeset lifecycle — accept, reject, re-evaluate, undo, reset.

Wiring, not logic: reads the World, hands validated operations to the
transactional applier in ``database/queries/worlds.py``, and records the
outcome. Three rules shape everything here.

**Exactly one accept wins a race.** Every mutation takes the per-World process
lock and then a ``BEGIN IMMEDIATE`` transaction that re-reads
``content_revision`` and refuses unless it still matches the proposal's base.
The loser is marked stale and told to re-evaluate; nothing is force-applied and
nothing is silently rebased, because two changes that look unrelated can still
contradict each other in meaning.

**Every mutation is itself a changeset.** Undo and reset are not special-cased
side doors — they build compensating operations, persist them as a changeset,
and apply them through the same path, so they land in history and can themselves
be undone.

**Undo never guesses.** A compensating operation carries the after-state its
target must still be in; if a later change moved it, the undo conflicts instead
of clobbering.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ... import database as db
from ...core import world_apply_lock
from ...database.models import WorldChangesetRow
from .proposals import validate_proposal

logger = logging.getLogger(__name__)

# Operations that mint a new overlay row; their inverse is "archive what it made".
_CREATING_OPS = ("create", "replace", "suppress")

# Fields an inverse `update` restores. Mirrors DYNAMIC_UPDATE_COLUMNS in the
# query layer minus the ones an overlay update never touches.
_RESTORED_FIELDS = ("name", "content", "keywords", "priority", "enabled")


def dynamic_enabled(world: Mapping[str, Any] | None) -> bool:
    """Whether *world* opts in to Agent-managed changes. ``None`` reads as off."""
    return bool(world and world.get("dynamic_enabled"))


async def stage_proposal(
    *,
    world_id: str,
    base_revision: int,
    summary: str,
    operations: Sequence[Mapping[str, Any]],
    source_user_message_id: int | None,
    source_assistant_message_id: int | None,
    source_conversation_id: str | None,
    source_character_label: str = "",
    source_conversation_label: str = "",
    supersedes_changeset_id: int | None = None,
    origin: str = "agent",
) -> WorldChangesetRow:
    """Persist a validated proposal as a pending changeset. Applies nothing."""
    return await db.create_world_changeset(
        {
            "world_id": world_id,
            "status": "pending",
            "base_revision": base_revision,
            "origin": origin,
            "summary": summary,
            "operations": list(operations),
            "source_user_message_id": source_user_message_id,
            "source_assistant_message_id": source_assistant_message_id,
            "source_conversation_id": source_conversation_id,
            "source_character_label": source_character_label,
            "source_conversation_label": source_conversation_label,
            "supersedes_changeset_id": supersedes_changeset_id,
        }
    )


async def supersede_proposal(
    changeset_id: int,
    replacement: Mapping[str, Any] | None,
) -> WorldChangesetRow | None:
    """Retire an open proposal and optionally stage its re-evaluated replacement.

    The query layer performs both writes in one ``BEGIN IMMEDIATE`` transaction,
    so two re-evaluations cannot create sibling replacements and a concurrent
    apply/reject cannot leave a replacement behind after winning the old row.
    """
    return await db.supersede_world_changeset(changeset_id, replacement)


async def accept_changeset(
    changeset: Mapping[str, Any],
    *,
    operations: Sequence[Mapping[str, Any]] | None = None,
    summary: str | None = None,
) -> WorldChangesetRow:
    """Apply a pending changeset atomically, re-validating first.

    *operations* overrides the stored list, which is how "edit then apply" works:
    the user may drop or reword individual operations, but whatever survives
    commits as one batch. The override is re-validated against the live World
    rather than trusted — the client is not the authority on which entry ids
    exist or which layer they are in.

    Raises :class:`db.RevisionConflict` when the World moved on (the caller marks
    the changeset stale and offers Re-evaluate) or
    :class:`db.OverlayStateConflict` when an operation no longer resolves.
    """
    world_id = changeset["world_id"]
    async with world_apply_lock(world_id):
        revision = await db.get_content_revision(world_id)
        if revision is None:
            raise db.OverlayStateConflict(f"world {world_id} not found")
        expected_revision = int(changeset["base_revision"])
        if revision != expected_revision:
            raise db.RevisionConflict(expected_revision, revision)
        entries = await db.get_lorebook_entries(world_id)
        proposed = list(operations if operations is not None else changeset["operations"])
        checked = validate_proposal(
            {"summary": summary or changeset["summary"], "operations": proposed},
            entries,
        )
        if checked.rejected:
            logger.info(
                "Changeset %s: dropped %d operation(s) on accept: %s",
                changeset["id"],
                len(checked.rejected),
                checked.rejected,
            )
        if not checked.operations:
            raise db.OverlayStateConflict("no operation in this changeset still applies to the world")
        return await db.apply_changeset(
            int(changeset["id"]),
            checked.operations,
            expected_revision=expected_revision,
            summary=checked.summary or None,
        )


async def reject_changeset(changeset: Mapping[str, Any]) -> WorldChangesetRow | None:
    """Reject an open changeset with an atomic status compare-and-swap."""
    return await db.update_world_changeset(
        int(changeset["id"]),
        {"status": "rejected", "decided_at": datetime.now(UTC).isoformat()},
        expected_statuses=("pending", "stale"),
    )


def invert_operations(
    operations: Sequence[Mapping[str, Any]],
    before_entries: Sequence[Mapping[str, Any] | None],
    after_entries: Sequence[Mapping[str, Any] | None],
) -> tuple[list[dict], list[dict | None]]:
    """Build the compensating operations for an applied changeset.

    Returns ``(inverse_ops, required_state)`` — positionally paired, where
    ``required_state`` is the after-snapshot each inverse op's target must still
    match for the undo to be safe.

    The inverses are read straight off the recorded snapshots, in reverse order
    so a proposal that created an entry and then updated it unwinds cleanly:

    * an operation that *created* an overlay row (create/replace/suppress) is
      undone by archiving that row, which re-exposes any authored entry it hid;
    * an ``update`` is undone by restoring the recorded before-values;
    * an ``archive`` is undone by restoring the recorded before-``archived`` flag.
    """
    inverse: list[dict] = []
    required: list[dict | None] = []
    for op, before, after in reversed(list(zip(operations, before_entries, after_entries, strict=False))):
        if after is None:
            continue
        entry_id = after.get("id")
        if entry_id is None:
            continue
        kind = op.get("op")
        if kind in _CREATING_OPS:
            inverse.append({"op": "archive", "target_entry_id": entry_id, "archived": True})
        elif kind == "update" and before is not None:
            restored: dict[str, Any] = {"op": "update", "target_entry_id": entry_id}
            restored.update({f: before[f] for f in _RESTORED_FIELDS if f in before})
            restored["activation"] = "constant" if before.get("constant") else "keywords"
            inverse.append(restored)
        elif kind == "archive" and before is not None:
            inverse.append(
                {
                    "op": "archive",
                    "target_entry_id": entry_id,
                    "archived": bool(before.get("archived")),
                }
            )
        else:
            continue
        required.append(dict(after))
    return inverse, required


async def undo_changeset(changeset: Mapping[str, Any]) -> WorldChangesetRow:
    """Reverse an applied changeset by applying its compensating changeset.

    Only reverses what is still there: each inverse operation carries the
    after-state it expects, so an entry a later change has moved on makes the
    whole undo conflict (``409``) rather than clobbering that later change.
    """
    world_id = changeset["world_id"]
    async with world_apply_lock(world_id):
        inverse, required = invert_operations(
            changeset["operations"],
            changeset["before_entries"],
            changeset["after_entries"],
        )
        if not inverse:
            raise db.OverlayStateConflict("this changeset made no reversible entry changes")
        revision = await db.get_content_revision(world_id)
        if revision is None:
            raise db.OverlayStateConflict(f"world {world_id} not found")
        return await db.create_and_apply_changeset(
            {
                "world_id": world_id,
                "status": "pending",
                "base_revision": revision,
                "origin": "undo",
                "summary": f"Undo: {changeset['summary']}" if changeset["summary"] else "Undo",
                "operations": inverse,
                "reverts_changeset_id": int(changeset["id"]),
                "source_user_message_id": changeset.get("source_user_message_id"),
                "source_assistant_message_id": changeset.get("source_assistant_message_id"),
                "source_conversation_id": changeset.get("source_conversation_id"),
                "source_character_label": changeset.get("source_character_label", ""),
                "source_conversation_label": changeset.get("source_conversation_label", ""),
            },
            inverse,
            expected_revision=revision,
            require_after_state=required,
            revert_changeset_id=int(changeset["id"]),
        )


async def reset_world_to_authored(world_id: str) -> WorldChangesetRow | None:
    """Archive every live overlay row, restoring the authored view exactly.

    Deterministic because the overlay never wrote an authored row: retiring all
    of it *is* the original. Recorded as an ordinary applied changeset, so the
    reset itself shows up in history and can be undone. Returns ``None`` when
    there was no overlay to retire.
    """
    async with world_apply_lock(world_id):
        active = await db.get_active_dynamic_entries(world_id)
        if not active:
            return None
        revision = await db.get_content_revision(world_id)
        if revision is None:
            raise db.OverlayStateConflict(f"world {world_id} not found")
        operations = [{"op": "archive", "target_entry_id": e["id"], "archived": True} for e in active]
        return await db.create_and_apply_changeset(
            {
                "world_id": world_id,
                "status": "pending",
                "base_revision": revision,
                "origin": "reset",
                "summary": f"Reset to authored world ({len(operations)} dynamic entr"
                f"{'y' if len(operations) == 1 else 'ies'} archived)",
                "operations": operations,
            },
            operations,
            expected_revision=revision,
        )


async def mark_stale(changeset_id: int) -> WorldChangesetRow | None:
    """Retire a pending proposal whose evidence or base World no longer holds."""
    return await db.update_world_changeset(
        changeset_id,
        {"status": "stale", "decided_at": datetime.now(UTC).isoformat()},
        expected_statuses=("pending", "stale"),
    )
