from __future__ import annotations

from typing import cast

from ..connection import _build_set_clause, get_db, immediate_tx
from ..models import InteractiveFragmentRow

_EDITOR_LANE_FIELD_TYPES = frozenset(("feedback", "post_processing"))


class InteractiveFragmentReorderLaneMismatch(ValueError):
    """A reorder attempted to mix the Director and Editor priority lanes."""


async def get_interactive_fragments() -> list[InteractiveFragmentRow]:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM interactive_fragments ORDER BY sort_order ASC, label ASC"))
        return [cast(InteractiveFragmentRow, dict(r)) for r in rows]


async def get_interactive_fragment(fid: str) -> InteractiveFragmentRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM interactive_fragments WHERE id = ?", (fid,)))
        return cast(InteractiveFragmentRow, dict(rows[0])) if rows else None


async def create_interactive_fragment(data: dict) -> InteractiveFragmentRow | None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO interactive_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order, direction_note_timing, cooldown_turns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["id"],
                data["label"],
                data["description"],
                data.get("field_type", "string"),
                1 if data.get("required", False) else 0,
                1 if data.get("enabled", True) else 0,
                data["injection_label"],
                data.get("sort_order", 0),
                data.get("direction_note_timing", "post_turn"),
                data.get("cooldown_turns", 0),
            ),
        )
        await db.commit()
        return await get_interactive_fragment(data["id"])


async def update_interactive_fragment(fid: str, data: dict) -> InteractiveFragmentRow | None:
    async with get_db() as db:
        allowed = [
            "label",
            "description",
            "field_type",
            "required",
            "enabled",
            "injection_label",
            "sort_order",
            "direction_note_timing",
            "cooldown_turns",
        ]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            vals.append(fid)
            await db.execute(
                f"UPDATE interactive_fragments SET {', '.join(sets)} WHERE id = ?",  # nosec B608 — cols from a hardcoded allowlist, values parameterised
                vals,
            )
            await db.commit()
        return await get_interactive_fragment(fid)


async def reorder_interactive_fragments(items: list[tuple[str, int]]) -> bool:
    """Atomically update a lane's existing fragment-priority slots.

    Callers send only the fragments in the reordered lane. All ids are checked
    while holding SQLite's write lock, so a stale, mixed-lane, or malformed
    batch cannot partly update priorities.
    """
    if not items:
        return True

    ids = [fid for fid, _ in items]
    placeholders = ", ".join("?" for _ in ids)
    async with immediate_tx() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT id, field_type FROM interactive_fragments WHERE id IN ({placeholders})",  # nosec B608 -- placeholder count derives solely from bound ids
                ids,
            )
        )
        if {str(row["id"]) for row in rows} != set(ids):
            return False
        lanes = {"editor" if row["field_type"] in _EDITOR_LANE_FIELD_TYPES else "director" for row in rows}
        if len(lanes) != 1:
            raise InteractiveFragmentReorderLaneMismatch("Fragments must belong to the same priority lane")
        await db.executemany(
            "UPDATE interactive_fragments SET sort_order = ? WHERE id = ?", [(order, fid) for fid, order in items]
        )
    return True


async def delete_interactive_fragment(fid: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM interactive_fragments WHERE id = ?", (fid,))
        await db.commit()
        return cur.rowcount > 0
