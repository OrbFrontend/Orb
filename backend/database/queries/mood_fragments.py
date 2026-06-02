from __future__ import annotations

from typing import cast

from ..connection import _build_set_clause, get_db
from ..models import MoodFragmentRow


async def get_mood_fragments() -> list[MoodFragmentRow]:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM mood_fragments ORDER BY label ASC"))
        return [cast(MoodFragmentRow, dict(r)) for r in rows]


async def get_mood_fragment(fid: str) -> MoodFragmentRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM mood_fragments WHERE id = ?", (fid,)))
        return cast(MoodFragmentRow, dict(rows[0])) if rows else None


async def create_mood_fragment(data: dict) -> MoodFragmentRow:
    async with get_db() as db:
        enabled = data.get("enabled", 1)
        await db.execute(
            "INSERT INTO mood_fragments (id, label, description, prompt_text, negative_prompt, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            (
                data["id"],
                data["label"],
                data["description"],
                data["prompt_text"],
                data.get("negative_prompt", ""),
                enabled,
            ),
        )
        await db.commit()
        result = await get_mood_fragment(data["id"])
        assert result is not None
        return result


async def update_mood_fragment(fid: str, data: dict) -> MoodFragmentRow | None:
    async with get_db() as db:
        allowed = ["label", "description", "prompt_text", "negative_prompt", "enabled"]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            vals.append(fid)
            await db.execute(
                f"UPDATE mood_fragments SET {', '.join(sets)} WHERE id = ?",
                vals,  # nosec B608 — cols from hardcoded allowlist, values parameterised
            )
            await db.commit()
        return await get_mood_fragment(fid)


async def delete_mood_fragment(fid: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM mood_fragments WHERE id = ?", (fid,))
        await db.commit()
        return cur.rowcount > 0
