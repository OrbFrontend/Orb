"""Store Worlds, lorebook entries, and changesets."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from ..connection import _build_set_clause, get_db, immediate_tx
from ..models import (
    ActiveLorebookEntryRow,
    LorebookEntryRow,
    WorldChangesetRow,
    WorldRow,
)

# The layer/action vocabulary, mirrored by the CHECK constraints in schema.py.
# Upgraded databases get these columns via ALTER, which cannot carry a CHECK, so
# these constants are the enforcement on that path too -- every writer below
# funnels values through them.
ENTRY_LAYERS = ("authored", "dynamic")
OVERLAY_ACTIONS = ("add", "replace", "suppress")
CHANGESET_STATUSES = (
    "pending",
    "applied",
    "rejected",
    "stale",
    "superseded",
    "reverted",
)
CHANGESET_ORIGINS = ("agent", "undo", "reset", "re_evaluate", "manual")

# Columns an entry INSERT names, in order. Shared by the authored and dynamic
# writers so a new column can never land on one path and not the other.
_ENTRY_INSERT_COLUMNS = (
    "world_id",
    "name",
    "content",
    "keywords",
    "case_insensitive",
    "constant",
    "at_depth",
    "use_regex",
    "selective",
    "secondary_keys",
    "priority",
    "enabled",
    "sort_order",
    "entry_layer",
    "entry_revision",
    "overlay_action",
    "supersedes_entry_id",
    "archived",
    "created_at",
    "updated_at",
)

# Entry columns an overlay `update` operation may rewrite. Deliberately narrow:
# the Agent-facing schema only chooses content/name/activation, and the rest keep
# whatever the reviewed apply set them to.
DYNAMIC_UPDATE_COLUMNS = (
    "name",
    "content",
    "keywords",
    "constant",
    "priority",
    "enabled",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def get_worlds() -> list[WorldRow]:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM worlds ORDER BY created_at ASC"))
        return [cast(WorldRow, dict(r)) for r in rows]


async def get_world(world_id: str) -> WorldRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM worlds WHERE id = ?", (world_id,)))
        return cast(WorldRow, dict(rows[0])) if rows else None


async def get_world_by_name(name: str) -> WorldRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM worlds WHERE name = ? LIMIT 1", (name,)))
        return cast(WorldRow, dict(rows[0])) if rows else None


async def create_world(data: dict) -> WorldRow:
    async with get_db() as db:
        now = _now()
        world_id = data.get("id") or str(uuid.uuid4())
        await db.execute(
            "INSERT INTO worlds (id, name, enabled, dynamic_enabled, content_revision, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 0, ?, ?)",
            (
                world_id,
                data["name"],
                1 if data.get("enabled", True) else 0,
                1 if data.get("dynamic_enabled", False) else 0,
                now,
                now,
            ),
        )
        await db.commit()
        result = await get_world(world_id)
        assert result is not None
        return result


async def update_world(world_id: str, data: dict) -> WorldRow | None:
    """Update a World's own fields. Never touches ``content_revision``.

    ``name``/``enabled``/``dynamic_enabled`` describe the container, not the
    lore, so none of them invalidates a pending proposal (see module docstring).
    """
    async with get_db() as db:
        allowed = ["name", "enabled", "dynamic_enabled"]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            sets.append("updated_at = ?")
            vals.append(_now())
            vals.append(world_id)
            await db.execute(
                f"UPDATE worlds SET {', '.join(sets)} WHERE id = ?",  # nosec B608 — cols from a hardcoded allowlist, values parameterised
                vals,
            )
            await db.commit()
        return await get_world(world_id)


async def disable_character_linked_worlds() -> list[str]:
    """Disable Worlds linked to character cards."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id FROM worlds WHERE enabled = 1"
                " AND id IN (SELECT world_id FROM character_cards WHERE world_id IS NOT NULL)"
            )
        )
        ids = [str(r[0]) for r in rows]
        if ids:
            await db.execute(
                f"UPDATE worlds SET enabled = 0 WHERE id IN ({', '.join('?' * len(ids))})",  # nosec B608 — placeholders only, values parameterised
                ids,
            )
            await db.commit()
        return ids


async def activate_character_linked_worlds(character_ids: Sequence[str]) -> list[str]:
    """Enable Worlds linked to *character_ids* without recency/revision stamps."""
    if not character_ids:
        return []
    placeholders = ", ".join("?" * len(character_ids))
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT DISTINCT world_id FROM character_cards "  # nosec B608 -- placeholders only
                f"WHERE id IN ({placeholders}) AND world_id IS NOT NULL ORDER BY world_id",
                list(character_ids),
            )
        )
        ids = [str(row[0]) for row in rows]
        if ids:
            await db.execute(
                f"UPDATE worlds SET enabled = 1 WHERE id IN ({', '.join('?' * len(ids))})",  # nosec B608
                ids,
            )
            await db.commit()
    return ids


async def delete_world(world_id: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM worlds WHERE id = ?", (world_id,))
        await db.commit()
        return cur.rowcount > 0


async def _bump_revision(db, world_id: str) -> int:
    """Advance *world_id*'s ``content_revision`` on an open connection, and return it.

    Takes the handle rather than opening its own so a multi-statement mutation
    (import, changeset apply) bumps exactly once inside its own transaction.
    """
    await db.execute(
        "UPDATE worlds SET content_revision = content_revision + 1, updated_at = ? WHERE id = ?",
        (_now(), world_id),
    )
    rows = list(await db.execute_fetchall("SELECT content_revision FROM worlds WHERE id = ?", (world_id,)))
    return int(rows[0][0]) if rows else 0


async def get_content_revision(world_id: str) -> int | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT content_revision FROM worlds WHERE id = ?", (world_id,)))
        return int(rows[0][0]) if rows else None


def _parse_lorebook_entry(row) -> LorebookEntryRow:
    d = dict(row)
    for col in ("keywords", "secondary_keys"):
        d[col] = json.loads(d[col]) if d.get(col) else []
    return cast(LorebookEntryRow, d)


def _entry_insert_values(world_id: str, data: Mapping[str, Any], now: str) -> tuple:
    """Bind one entry row's INSERT parameters, in ``_ENTRY_INSERT_COLUMNS`` order."""
    layer = data.get("entry_layer", "authored")
    if layer not in ENTRY_LAYERS:
        raise ValueError(f"unknown entry_layer {layer!r}")
    action = data.get("overlay_action", "") or ""
    if layer == "authored":
        # An authored row is never an overlay: the two columns only mean
        # something together, so an authored row carries neither.
        action, supersedes = "", None
    else:
        if action not in OVERLAY_ACTIONS:
            raise ValueError(f"dynamic entry needs an overlay_action, got {action!r}")
        supersedes = data.get("supersedes_entry_id")
    return (
        world_id,
        data["name"],
        data.get("content", ""),
        json.dumps(data.get("keywords", [])),
        1 if data.get("case_insensitive", True) else 0,
        1 if data.get("constant", False) else 0,
        1 if data.get("at_depth", False) else 0,
        1 if data.get("use_regex", False) else 0,
        1 if data.get("selective", False) else 0,
        json.dumps(data.get("secondary_keys", [])),
        data.get("priority", 100),
        1 if data.get("enabled", True) else 0,
        data.get("sort_order", 0),
        layer,
        int(data.get("entry_revision", 0)),
        action,
        supersedes,
        1 if data.get("archived", False) else 0,
        now,
        now,
    )


_ENTRY_INSERT_SQL = (
    f"INSERT INTO lorebook_entries ({', '.join(_ENTRY_INSERT_COLUMNS)})"  # nosec B608 — column names are a module constant
    f" VALUES ({', '.join('?' * len(_ENTRY_INSERT_COLUMNS))})"
)


async def _insert_entry(db, world_id: str, data: Mapping[str, Any], now: str) -> int:
    cur = await db.execute(_ENTRY_INSERT_SQL, _entry_insert_values(world_id, data, now))
    assert cur.lastrowid is not None
    return cur.lastrowid


async def _fetch_entry(db, entry_id: int) -> LorebookEntryRow | None:
    rows = list(await db.execute_fetchall("SELECT * FROM lorebook_entries WHERE id = ?", (entry_id,)))
    return _parse_lorebook_entry(rows[0]) if rows else None


async def get_lorebook_entries(world_id: str) -> list[LorebookEntryRow]:
    """Every row in the World, both layers, archived included.

    The drawer labels the layers and shows archived overlay rows, and the
    projection (``prompting.lorebook.select_effective_entries``) filters from
    this same superset -- so this reader stays deliberately unfiltered.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT * FROM lorebook_entries WHERE world_id = ? ORDER BY sort_order ASC, id ASC",
                (world_id,),
            )
        )
        return [_parse_lorebook_entry(r) for r in rows]


async def get_lorebook_entry(entry_id: int) -> LorebookEntryRow | None:
    async with get_db() as db:
        return await _fetch_entry(db, entry_id)


async def create_lorebook_entry(world_id: str, data: dict) -> LorebookEntryRow:
    """Create one entry (authored unless *data* says otherwise) and bump the revision."""
    async with get_db() as db:
        now = _now()
        entry_id = await _insert_entry(db, world_id, data, now)
        await _bump_revision(db, world_id)
        await db.commit()
        result = await _fetch_entry(db, entry_id)
        assert result is not None
        return result


async def import_lorebook_entries(world_id: str, items: Sequence[Mapping[str, Any]]) -> list[LorebookEntryRow]:
    """Insert a whole imported book in one transaction, bumping the revision once.

    A per-entry commit would publish a half-imported World to any concurrent
    reader and advance the revision once per row, invalidating pending proposals
    N times for what is a single user action.
    """
    async with get_db() as db:
        now = _now()
        ids = [await _insert_entry(db, world_id, item, now) for item in items]
        if ids:
            await _bump_revision(db, world_id)
        await db.commit()
        rows = [await _fetch_entry(db, i) for i in ids]
        return [r for r in rows if r is not None]


async def update_lorebook_entry(entry_id: int, data: dict) -> LorebookEntryRow | None:
    async with get_db() as db:
        existing = await _fetch_entry(db, entry_id)
        if existing is None:
            return None
        allowed = [
            "name",
            "content",
            "keywords",
            "case_insensitive",
            "constant",
            "at_depth",
            "use_regex",
            "selective",
            "secondary_keys",
            "priority",
            "enabled",
            "sort_order",
        ]
        sets, vals = _build_set_clause(allowed, data, json_fields={"keywords", "secondary_keys"})
        if sets:
            # Drawer edits of an Agent-managed row are later mutations too. The
            # undo guard compares this monotonic stamp, so even an edit to a
            # field the Agent schema never writes cannot be mistaken for the
            # exact after-state an older changeset recorded.
            if existing["entry_layer"] == "dynamic":
                sets.append("entry_revision = entry_revision + 1")
            sets.append("updated_at = ?")
            vals.append(_now())
            vals.append(entry_id)
            await db.execute(
                f"UPDATE lorebook_entries SET {', '.join(sets)} WHERE id = ?",  # nosec B608 — cols from a hardcoded allowlist, values parameterised
                vals,
            )
            await _bump_revision(db, existing["world_id"])
            await db.commit()
        return await _fetch_entry(db, entry_id)


async def delete_lorebook_entry(entry_id: int, *, record_as: Mapping[str, Any] | None = None) -> bool:
    """Delete one lorebook entry and record its history."""
    async with immediate_tx() as db:
        existing = await _fetch_entry(db, entry_id)
        cur = await db.execute("DELETE FROM lorebook_entries WHERE id = ?", (entry_id,))
        if cur.rowcount == 0 or existing is None:
            return cur.rowcount > 0
        revision = await _bump_revision(db, existing["world_id"])
        if record_as is not None:
            now = _now()
            await _insert_changeset(
                db,
                {
                    **record_as,
                    "world_id": existing["world_id"],
                    "status": "applied",
                    # The bump above is this transaction's own +1, so the World
                    # the delete acted on is exactly one revision behind.
                    "base_revision": revision - 1,
                    "applied_revision": revision,
                    "before_entries": [entry_snapshot(existing)],
                    "after_entries": [None],
                    "created_at": now,
                    "decided_at": now,
                    "applied_at": now,
                },
            )
        return True


async def get_active_lorebook_entries() -> list[ActiveLorebookEntryRow]:
    """Enabled, non-archived entries from enabled worlds -- **both** layers.

    Joins ``w.name AS world_name`` so callers (the agentic-lorebook catalog) can
    group entries by their world. The extra key is additive -- readers of the
    base ``LorebookEntryRow`` columns are unaffected.

    This is the raw overlay pool, not the effective lore: an authored entry
    hidden by a replacement is still in here, and so is the suppression marker
    that hides it. Resolving that is ``prompting.lorebook`` -- the projection
    lives in the lore layer, which ``database/`` sits below and may not import.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                """
            SELECT le.*, w.name AS world_name FROM lorebook_entries le
            JOIN worlds w ON le.world_id = w.id
            WHERE le.enabled = 1 AND w.enabled = 1 AND le.archived = 0
            ORDER BY le.priority DESC, le.sort_order ASC, le.id ASC
            """
            )
        )
        return [cast(ActiveLorebookEntryRow, _parse_lorebook_entry(r)) for r in rows]


def _parse_changeset(row) -> WorldChangesetRow:
    d = dict(row)
    for col in ("operations", "before_entries", "after_entries"):
        d[col] = json.loads(d[col]) if d.get(col) else []
    return cast(WorldChangesetRow, d)


async def _fetch_changeset(db, changeset_id: int) -> WorldChangesetRow | None:
    rows = list(await db.execute_fetchall("SELECT * FROM world_changesets WHERE id = ?", (changeset_id,)))
    return _parse_changeset(rows[0]) if rows else None


def _validate_changeset_metadata(data: Mapping[str, Any]) -> tuple[str, str]:
    status = data.get("status", "pending")
    if status not in CHANGESET_STATUSES:
        raise ValueError(f"unknown changeset status {status!r}")
    origin = data.get("origin", "agent")
    if origin not in CHANGESET_ORIGINS:
        raise ValueError(f"unknown changeset origin {origin!r}")
    return str(status), str(origin)


async def _insert_changeset(db, data: Mapping[str, Any]) -> int:
    """Insert a changeset on the caller's transaction and return its id."""
    status, origin = _validate_changeset_metadata(data)
    cur = await db.execute(
        """
        INSERT INTO world_changesets (
            world_id, status, base_revision, applied_revision,
            source_user_message_id, source_assistant_message_id, source_conversation_id,
            source_character_label, source_conversation_label, origin, summary,
            operations, before_entries, after_entries,
            reverts_changeset_id, supersedes_changeset_id,
            created_at, decided_at, applied_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["world_id"],
            status,
            int(data.get("base_revision", 0)),
            data.get("applied_revision"),
            data.get("source_user_message_id"),
            data.get("source_assistant_message_id"),
            data.get("source_conversation_id"),
            data.get("source_character_label", "") or "",
            data.get("source_conversation_label", "") or "",
            origin,
            data.get("summary", "") or "",
            json.dumps(data.get("operations", [])),
            json.dumps(data.get("before_entries", [])),
            json.dumps(data.get("after_entries", [])),
            data.get("reverts_changeset_id"),
            data.get("supersedes_changeset_id"),
            data.get("created_at") or _now(),
            data.get("decided_at"),
            data.get("applied_at"),
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


async def create_world_changeset(data: Mapping[str, Any]) -> WorldChangesetRow:
    """Persist a changeset row (pending proposal, or an already-decided record)."""
    async with get_db() as db:
        changeset_id = await _insert_changeset(db, data)
        await db.commit()
        result = await _fetch_changeset(db, changeset_id)
        assert result is not None
        return result


async def supersede_world_changeset(
    changeset_id: int,
    replacement: Mapping[str, Any] | None,
) -> WorldChangesetRow | None:
    """Atomically retire an open changeset and optionally insert its replacement.

    Re-evaluation spends an LLM call before it knows whether there is still a
    useful proposal. The old row must remain open during that call, but the
    eventual status transition and replacement INSERT are one database decision:
    a concurrent apply/reject/re-evaluate either wins first, or this transaction
    wins without leaking a second pending replacement.

    ``None`` means the re-evaluation found nothing left to propose; the original
    still becomes terminal ``superseded`` so it leaves the review queue.
    """
    async with immediate_tx() as db:
        original = await _fetch_changeset(db, changeset_id)
        if original is None or original["status"] not in ("pending", "stale"):
            state = original["status"] if original else "missing"
            raise OverlayStateConflict(f"changeset {changeset_id} is {state}, not open for re-evaluation")

        cur = await db.execute(
            "UPDATE world_changesets SET status = 'superseded', decided_at = ? WHERE id = ? AND status IN ('pending', 'stale')",
            (_now(), changeset_id),
        )
        if cur.rowcount != 1:
            raise OverlayStateConflict(f"changeset {changeset_id} changed state while it was being re-evaluated")

        if replacement is None:
            return None
        if replacement.get("world_id") != original["world_id"]:
            raise OverlayStateConflict("a re-evaluated changeset must belong to the same world as its original")
        replacement_id = await _insert_changeset(
            db, {**replacement, "status": "pending", "supersedes_changeset_id": changeset_id}
        )
        result = await _fetch_changeset(db, replacement_id)
        assert result is not None
        return result


async def get_world_changeset(changeset_id: int) -> WorldChangesetRow | None:
    async with get_db() as db:
        return await _fetch_changeset(db, changeset_id)


async def get_world_changesets(world_id: str, *, statuses: Sequence[str] | None = None) -> list[WorldChangesetRow]:
    """Changesets for a World, newest first, optionally narrowed to *statuses*."""
    sql = "SELECT * FROM world_changesets WHERE world_id = ?"
    params: list[Any] = [world_id]
    if statuses:
        unknown = [s for s in statuses if s not in CHANGESET_STATUSES]
        if unknown:
            raise ValueError(f"unknown changeset status(es) {unknown!r}")
        sql += f" AND status IN ({', '.join('?' * len(statuses))})"  # nosec B608 — values parameterised, count only
        params.extend(statuses)
    sql += " ORDER BY id DESC"
    async with get_db() as db:
        rows = list(await db.execute_fetchall(sql, tuple(params)))
        return [_parse_changeset(r) for r in rows]


async def count_pending_changesets(
    world_ids: Sequence[str] | None = None,
) -> dict[str, int]:
    """``{world_id: count awaiting review}``, for the World list badge.

    Counts ``stale`` alongside ``pending``: a stale proposal is not a decision,
    it is one the user still has to make (by re-evaluating or dismissing), so
    hiding it from the badge would strand it. One grouped query, not one per
    World -- the badge costs the same with fifty lorebooks as with two.
    """
    sql = "SELECT world_id, COUNT(*) FROM world_changesets WHERE status IN ('pending', 'stale')"
    params: tuple = ()
    if world_ids is not None:
        if not world_ids:
            return {}
        sql += f" AND world_id IN ({', '.join('?' * len(world_ids))})"  # nosec B608 — values parameterised, count only
        params = tuple(world_ids)
    sql += " GROUP BY world_id"
    async with get_db() as db:
        rows = list(await db.execute_fetchall(sql, params))
        return {str(r[0]): int(r[1]) for r in rows}


async def get_changesets_for_messages(
    message_ids: Sequence[int],
) -> list[WorldChangesetRow]:
    """Every changeset sourced from one of *message_ids* -- one batched query.

    Backs the active-path message projection, which would otherwise issue a
    per-message lookup while painting a conversation.
    """
    if not message_ids:
        return []
    placeholders = ", ".join("?" * len(message_ids))
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT * FROM world_changesets WHERE source_assistant_message_id IN ({placeholders}) ORDER BY id DESC",  # nosec B608 — values parameterised, count only
                tuple(message_ids),
            )
        )
        return [_parse_changeset(r) for r in rows]


async def update_world_changeset(
    changeset_id: int,
    data: Mapping[str, Any],
    *,
    expected_statuses: Sequence[str] | None = None,
) -> WorldChangesetRow | None:
    """Patch a changeset, optionally as a compare-and-swap status transition.

    Decision routes pass ``expected_statuses`` so a stale request snapshot can
    never overwrite a concurrent accept/reject. SQLite evaluates the predicate
    when the write lock is actually held, making the transition authoritative
    rather than relying on a route-level pre-check.
    """
    allowed = [
        "status",
        "summary",
        "operations",
        "before_entries",
        "after_entries",
        "applied_revision",
        "decided_at",
        "applied_at",
        "reverts_changeset_id",
        "supersedes_changeset_id",
    ]
    if "status" in data and data["status"] not in CHANGESET_STATUSES:
        raise ValueError(f"unknown changeset status {data['status']!r}")
    if expected_statuses:
        unknown = [s for s in expected_statuses if s not in CHANGESET_STATUSES]
        if unknown:
            raise ValueError(f"unknown expected changeset status(es) {unknown!r}")
    async with get_db() as db:
        sets, vals = _build_set_clause(
            allowed,
            dict(data),
            json_fields={"operations", "before_entries", "after_entries"},
        )
        if sets:
            where = "id = ?"
            vals.append(changeset_id)
            if expected_statuses:
                where += f" AND status IN ({', '.join('?' * len(expected_statuses))})"
                vals.extend(expected_statuses)
            cur = await db.execute(
                f"UPDATE world_changesets SET {', '.join(sets)} WHERE {where}",  # nosec B608 — cols/status count from hardcoded allowlists
                vals,
            )
            if expected_statuses and cur.rowcount != 1:
                await db.rollback()
                current = await _fetch_changeset(db, changeset_id)
                state = current["status"] if current else "missing"
                raise OverlayStateConflict(f"changeset {changeset_id} is {state}, not open for this action")
            await db.commit()
        return await _fetch_changeset(db, changeset_id)


async def mark_orphaned_changesets_stale() -> int:
    """Mark proposals stale when their source messages are gone."""
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE world_changesets SET status = 'stale', decided_at = ?"
            " WHERE status = 'pending' AND source_assistant_message_id IS NULL",
            (_now(),),
        )
        await db.commit()
        return cur.rowcount


async def mark_changesets_stale_for_messages(message_ids: Sequence[int]) -> int:
    """Mark every *pending* proposal sourced from one of *message_ids* stale.

    Called when a source message is edited or deleted: the evidence the proposal
    was derived from no longer exists as the model saw it, so the proposal must
    be re-evaluated rather than applied. Applied history is untouched, for the
    reason :func:`mark_orphaned_changesets_stale` gives.
    """
    if not message_ids:
        return 0
    placeholders = ", ".join("?" * len(message_ids))
    async with get_db() as db:
        cur = await db.execute(
            f"UPDATE world_changesets SET status = 'stale', decided_at = ? WHERE status = 'pending'"  # nosec B608 — values parameterised, count only
            f" AND (source_assistant_message_id IN ({placeholders}) OR source_user_message_id IN ({placeholders}))",
            (_now(), *message_ids, *message_ids),
        )
        await db.commit()
        return cur.rowcount


class RevisionConflict(RuntimeError):
    """The World moved on since the changeset was proposed; nothing was applied."""

    def __init__(self, expected: int, actual: int):
        super().__init__(f"world content_revision is {actual}, changeset expects {expected}")
        self.expected = expected
        self.actual = actual


class OverlayStateConflict(RuntimeError):
    """A targeted overlay row no longer matches the state the caller recorded."""


def entry_snapshot(entry: Mapping[str, Any] | None) -> dict | None:
    """The comparable subset of an entry row stored in a changeset's before/after.

    Timestamps are excluded on purpose: undo compares an after-snapshot with the
    live row to decide whether the change is still safe to reverse, and
    ``updated_at`` would make every comparison fail.
    """
    if entry is None:
        return None
    return {
        "id": entry["id"],
        "world_id": entry["world_id"],
        "name": entry["name"],
        "content": entry["content"],
        "keywords": list(entry.get("keywords") or []),
        "case_insensitive": int(bool(entry.get("case_insensitive", 1))),
        "constant": int(bool(entry.get("constant"))),
        "at_depth": int(bool(entry.get("at_depth"))),
        "use_regex": int(bool(entry.get("use_regex"))),
        "selective": int(bool(entry.get("selective"))),
        "secondary_keys": list(entry.get("secondary_keys") or []),
        "priority": int(entry.get("priority", 100)),
        "enabled": int(bool(entry.get("enabled", 1))),
        "sort_order": int(entry.get("sort_order", 0)),
        "entry_layer": entry.get("entry_layer", "authored"),
        "entry_revision": int(entry.get("entry_revision", 0)),
        "overlay_action": entry.get("overlay_action", ""),
        "supersedes_entry_id": entry.get("supersedes_entry_id"),
        "archived": int(bool(entry.get("archived"))),
    }


def _after_state_matches(live: Mapping[str, Any] | None, expected: Mapping[str, Any]) -> bool:
    """Whether *live* is still the row an undo recorded, tolerating a lost target.

    Snapshot equality, with exactly one carve-out. Deleting an authored entry
    SET-NULLs the ``supersedes_entry_id`` of every overlay row that hid it (see
    schema.py), which turns a ``replace`` into a standalone ``add`` and neuters a
    ``suppress``. That is the user's own delete showing through the pointer, not
    a later edit to the overlay row the undo would clobber -- and refusing on it
    would strand an applied changeset with an Undo button that can never
    succeed. Every other field, ``entry_revision`` included, must still match.
    """
    snapshot = entry_snapshot(live)
    if snapshot is None:
        return False
    expected = dict(expected)
    if snapshot == expected:
        return True
    lost_target = snapshot.get("supersedes_entry_id") is None and expected.get("supersedes_entry_id") is not None
    return lost_target and {**snapshot, "supersedes_entry_id": expected["supersedes_entry_id"]} == expected


async def _apply_one(db, world_id: str, op: Mapping[str, Any], now: str) -> tuple[dict | None, dict | None]:
    """Execute one validated operation. Returns ``(before, after)`` snapshots.

    Every branch is expressed as an insert or an update of a *dynamic* row. No
    branch writes an authored row -- that invariant is what makes "Reset to
    Authored World" a deterministic restore rather than a best-effort undo.
    """
    kind = op["op"]

    if kind in ("create", "replace", "suppress"):
        target_id = op.get("target_entry_id") if kind != "create" else None
        if kind != "create":
            target = await _fetch_entry(db, int(target_id or 0))
            if target is None or target["world_id"] != world_id:
                raise OverlayStateConflict(f"{kind} target entry {target_id} is not in this world")
            if target["entry_layer"] != "authored":
                raise OverlayStateConflict(f"{kind} may only target an authored entry (got {target['entry_layer']})")
        row = {
            "name": op.get("name", ""),
            # A suppression injects nothing; it exists only to hide its target,
            # so it carries no content and the projection drops it before render.
            "content": "" if kind == "suppress" else op.get("content", ""),
            "keywords": list(op.get("keywords") or []),
            "constant": bool(op.get("activation") == "constant"),
            "priority": int(op.get("priority", 100)),
            "enabled": True,
            "entry_layer": "dynamic",
            "overlay_action": "add" if kind == "create" else kind,
            "supersedes_entry_id": int(target_id) if target_id is not None else None,
        }
        new_id = await _insert_entry(db, world_id, row, now)
        return None, entry_snapshot(await _fetch_entry(db, new_id))

    entry_id = int(op.get("target_entry_id") or 0)
    existing = await _fetch_entry(db, entry_id)
    if existing is None or existing["world_id"] != world_id:
        raise OverlayStateConflict(f"{kind} target entry {entry_id} is not in this world")
    if existing["entry_layer"] != "dynamic":
        raise OverlayStateConflict(f"{kind} may only target a dynamic entry (got {existing['entry_layer']})")
    before = entry_snapshot(existing)

    if kind == "update":
        patch: dict[str, Any] = {col: op[col] for col in DYNAMIC_UPDATE_COLUMNS if col in op}
        if "activation" in op:
            patch["constant"] = op["activation"] == "constant"
            if patch["constant"]:
                patch["keywords"] = []
        for flag in ("constant", "enabled"):
            if flag in patch:
                patch[flag] = 1 if patch[flag] else 0
        sets, vals = _build_set_clause(list(patch), patch, json_fields={"keywords"})
        sets.extend(["entry_revision = entry_revision + 1", "updated_at = ?"])
        vals.extend([now, entry_id])
        await db.execute(
            f"UPDATE lorebook_entries SET {', '.join(sets)} WHERE id = ?",  # nosec B608 — cols from a hardcoded allowlist, values parameterised
            vals,
        )
    elif kind == "archive":
        await db.execute(
            "UPDATE lorebook_entries SET archived = ?, entry_revision = entry_revision + 1, updated_at = ? WHERE id = ?",
            (1 if op.get("archived", True) else 0, now, entry_id),
        )
    else:
        raise ValueError(f"unknown operation {kind!r}")

    return before, entry_snapshot(await _fetch_entry(db, entry_id))


async def _apply_changeset_tx(
    db,
    changeset_id: int,
    operations: Sequence[Mapping[str, Any]],
    *,
    expected_revision: int,
    summary: str | None = None,
    require_after_state: Sequence[Mapping[str, Any] | None] | None = None,
) -> WorldChangesetRow:
    """Apply one already-persisted pending changeset on an open IMMEDIATE transaction."""
    changeset = await _fetch_changeset(db, changeset_id)
    if changeset is None:
        raise OverlayStateConflict(f"changeset {changeset_id} not found")
    if changeset["status"] != "pending":
        raise OverlayStateConflict(f"changeset {changeset_id} is {changeset['status']}; only pending changes can be applied")
    if int(changeset["base_revision"]) != expected_revision:
        raise OverlayStateConflict(
            f"changeset {changeset_id} expects revision {changeset['base_revision']}, not {expected_revision}"
        )
    world_id = changeset["world_id"]

    rows = list(await db.execute_fetchall("SELECT content_revision FROM worlds WHERE id = ?", (world_id,)))
    if not rows:
        raise OverlayStateConflict(f"world {world_id} not found")
    actual = int(rows[0][0])
    if actual != expected_revision:
        raise RevisionConflict(expected_revision, actual)

    if require_after_state is not None:
        for op, expected_state in zip(operations, require_after_state, strict=True):
            if expected_state is None:
                continue
            live = await _fetch_entry(db, int(op.get("target_entry_id") or 0))
            if not _after_state_matches(live, expected_state):
                gone = live is None
                raise OverlayStateConflict(
                    f"entry {op.get('target_entry_id')} was deleted since this changeset was applied"
                    if gone
                    else f"entry {op.get('target_entry_id')} changed since this changeset was applied"
                )

    now = _now()
    befores: list[dict | None] = []
    afters: list[dict | None] = []
    for op in operations:
        before, after = await _apply_one(db, world_id, op, now)
        befores.append(before)
        afters.append(after)

    new_revision = await _bump_revision(db, world_id)
    cur = await db.execute(
        "UPDATE world_changesets SET status = 'applied', operations = ?, before_entries = ?,"
        " after_entries = ?, applied_revision = ?, summary = COALESCE(?, summary),"
        " decided_at = COALESCE(decided_at, ?), applied_at = ? WHERE id = ? AND status = 'pending'",
        (
            json.dumps(list(operations)),
            json.dumps(befores),
            json.dumps(afters),
            new_revision,
            summary,
            now,
            now,
            changeset_id,
        ),
    )
    if cur.rowcount != 1:
        raise OverlayStateConflict(f"changeset {changeset_id} changed state while it was being applied")
    result = await _fetch_changeset(db, changeset_id)
    assert result is not None
    return result


async def apply_changeset(
    changeset_id: int,
    operations: Sequence[Mapping[str, Any]],
    *,
    expected_revision: int,
    summary: str | None = None,
    require_after_state: Sequence[Mapping[str, Any] | None] | None = None,
) -> WorldChangesetRow:
    """Apply *operations* atomically, or raise and leave the World untouched.

    Re-reads ``content_revision`` inside the write transaction and refuses
    unless it still equals *expected_revision* -- exactly one accept can win a
    race, the loser gets a :class:`RevisionConflict` and is marked stale by the
    caller.

    *require_after_state* is the undo guard: a list positionally matching
    *operations*, each entry the snapshot the compensating op expects to find
    live. A mismatch raises :class:`OverlayStateConflict` and applies nothing,
    so an undo can never silently clobber a later edit.
    """
    async with immediate_tx() as db:
        return await _apply_changeset_tx(
            db,
            changeset_id,
            operations,
            expected_revision=expected_revision,
            summary=summary,
            require_after_state=require_after_state,
        )


async def create_and_apply_changeset(
    data: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    *,
    expected_revision: int,
    require_after_state: Sequence[Mapping[str, Any] | None] | None = None,
    revert_changeset_id: int | None = None,
) -> WorldChangesetRow:
    """Create and apply an internal undo/reset record in one transaction.

    A failed state guard or revision check rolls back the INSERT as well as the
    overlay writes, so an internal compensating operation can never leak into
    the ordinary pending review queue. When undoing, the original history row is
    marked ``reverted`` in this same commit.
    """
    if data.get("status", "pending") != "pending":
        raise ValueError("create_and_apply_changeset requires a pending changeset")
    async with immediate_tx() as db:
        if revert_changeset_id is not None:
            original = await _fetch_changeset(db, revert_changeset_id)
            if original is None or original["status"] != "applied":
                state = original["status"] if original else "missing"
                raise OverlayStateConflict(f"changeset {revert_changeset_id} is {state}; only applied changes can be undone")
            if original["world_id"] != data["world_id"]:
                raise OverlayStateConflict("an undo changeset must belong to the same world as its original")

        changeset_id = await _insert_changeset(db, data)
        result = await _apply_changeset_tx(
            db,
            changeset_id,
            operations,
            expected_revision=expected_revision,
            require_after_state=require_after_state,
        )
        if revert_changeset_id is not None:
            cur = await db.execute(
                "UPDATE world_changesets SET status = 'reverted' WHERE id = ? AND status = 'applied'",
                (revert_changeset_id,),
            )
            if cur.rowcount != 1:
                raise OverlayStateConflict(f"changeset {revert_changeset_id} changed state while it was being undone")
        return result


async def get_active_dynamic_entries(world_id: str) -> list[LorebookEntryRow]:
    """The World's live (non-archived) overlay rows -- what a reset would retire."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT * FROM lorebook_entries WHERE world_id = ? AND entry_layer = 'dynamic' AND archived = 0"
                " ORDER BY sort_order ASC, id ASC",
                (world_id,),
            )
        )
        return [_parse_lorebook_entry(r) for r in rows]
