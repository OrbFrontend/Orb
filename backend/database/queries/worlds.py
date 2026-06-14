from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import cast

from ..connection import _build_set_clause, get_db
from ..models import ActiveLorebookEntryRow, LorebookEntryRow, WorldRow


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
        now = datetime.now(timezone.utc).isoformat()
        world_id = data.get("id") or str(uuid.uuid4())
        await db.execute(
            "INSERT INTO worlds (id, name, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                world_id,
                data["name"],
                1 if data.get("enabled", True) else 0,
                now,
                now,
            ),
        )
        await db.commit()
        result = await get_world(world_id)
        assert result is not None
        return result


async def update_world(world_id: str, data: dict) -> WorldRow | None:
    async with get_db() as db:
        allowed = ["name", "enabled"]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            sets.append("updated_at = ?")
            vals.append(datetime.now(timezone.utc).isoformat())
            vals.append(world_id)
            await db.execute(
                f"UPDATE worlds SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            await db.commit()
        return await get_world(world_id)


async def delete_world(world_id: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM worlds WHERE id = ?", (world_id,))
        await db.commit()
        return cur.rowcount > 0


def _parse_lorebook_entry(row) -> LorebookEntryRow:
    d = dict(row)
    d["keywords"] = json.loads(d["keywords"]) if d.get("keywords") else []
    return cast(LorebookEntryRow, d)


async def get_lorebook_entries(world_id: str) -> list[LorebookEntryRow]:
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
        rows = list(await db.execute_fetchall("SELECT * FROM lorebook_entries WHERE id = ?", (entry_id,)))
        return _parse_lorebook_entry(rows[0]) if rows else None


async def create_lorebook_entry(world_id: str, data: dict) -> LorebookEntryRow:
    async with get_db() as db:
        now = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            "INSERT INTO lorebook_entries (world_id, name, content, keywords, case_insensitive, constant, priority, enabled, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                world_id,
                data["name"],
                data.get("content", ""),
                json.dumps(data.get("keywords", [])),
                1 if data.get("case_insensitive", True) else 0,
                1 if data.get("constant", False) else 0,
                data.get("priority", 100),
                1 if data.get("enabled", True) else 0,
                data.get("sort_order", 0),
                now,
                now,
            ),
        )
        assert cur.lastrowid is not None
        await db.commit()
        result = await get_lorebook_entry(cur.lastrowid)
        assert result is not None
        return result


async def update_lorebook_entry(entry_id: int, data: dict) -> LorebookEntryRow | None:
    async with get_db() as db:
        allowed = [
            "name",
            "content",
            "keywords",
            "case_insensitive",
            "constant",
            "priority",
            "enabled",
            "sort_order",
        ]
        sets, vals = _build_set_clause(allowed, data, json_fields={"keywords"})
        if sets:
            sets.append("updated_at = ?")
            vals.append(datetime.now(timezone.utc).isoformat())
            vals.append(entry_id)
            await db.execute(
                f"UPDATE lorebook_entries SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            await db.commit()
        return await get_lorebook_entry(entry_id)


async def delete_lorebook_entry(entry_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM lorebook_entries WHERE id = ?", (entry_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_active_lorebook_entries() -> list[ActiveLorebookEntryRow]:
    """Return all enabled entries from enabled worlds, ordered by priority DESC, sort_order ASC.

    Joins ``w.name AS world_name`` so callers (the agentic-lorebook catalog) can
    group entries by their world. The extra key is additive — readers of the
    base ``LorebookEntryRow`` columns are unaffected.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                """
            SELECT le.*, w.name AS world_name FROM lorebook_entries le
            JOIN worlds w ON le.world_id = w.id
            WHERE le.enabled = 1 AND w.enabled = 1
            ORDER BY le.priority DESC, le.sort_order ASC, le.id ASC
            """
            )
        )
        return [cast(ActiveLorebookEntryRow, _parse_lorebook_entry(r)) for r in rows]
