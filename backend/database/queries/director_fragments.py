from __future__ import annotations

from typing import cast

from ..connection import _build_set_clause, get_db
from ..models import DirectorFragmentRow


async def get_director_fragments() -> list[DirectorFragmentRow]:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM director_fragments ORDER BY sort_order ASC, label ASC"))
        return [cast(DirectorFragmentRow, dict(r)) for r in rows]


async def get_director_fragment(fid: str) -> DirectorFragmentRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM director_fragments WHERE id = ?", (fid,)))
        return cast(DirectorFragmentRow, dict(rows[0])) if rows else None


async def create_director_fragment(data: dict) -> DirectorFragmentRow | None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO director_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["id"],
                data["label"],
                data["description"],
                data.get("field_type", "string"),
                1 if data.get("required", False) else 0,
                1 if data.get("enabled", True) else 0,
                data["injection_label"],
                data.get("sort_order", 0),
            ),
        )
        await db.commit()
        return await get_director_fragment(data["id"])


async def update_director_fragment(fid: str, data: dict) -> DirectorFragmentRow | None:
    async with get_db() as db:
        allowed = [
            "label",
            "description",
            "field_type",
            "required",
            "enabled",
            "injection_label",
            "sort_order",
        ]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            vals.append(fid)
            await db.execute(
                f"UPDATE director_fragments SET {', '.join(sets)} WHERE id = ?",
                vals,  # nosec B608 — cols from hardcoded allowlist, values parameterised
            )
            await db.commit()
        return await get_director_fragment(fid)


async def delete_director_fragment(fid: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM director_fragments WHERE id = ?", (fid,))
        await db.commit()
        return cur.rowcount > 0
