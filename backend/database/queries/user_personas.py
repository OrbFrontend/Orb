from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

from ..connection import _build_set_clause, get_db
from ..models import UserPersonaRow


async def get_user_personas() -> list[UserPersonaRow]:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, name, description, avatar_color, created_at, updated_at FROM user_personas ORDER BY name ASC"
            )
        )
        return [cast(UserPersonaRow, dict(r)) for r in rows]


async def get_user_persona(persona_id: int) -> UserPersonaRow | None:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, name, description, avatar_color, created_at, updated_at FROM user_personas WHERE id = ?",
                (persona_id,),
            )
        )
        return cast(UserPersonaRow, dict(rows[0])) if rows else None


async def create_user_persona(data: dict) -> UserPersonaRow:
    async with get_db() as db:
        now = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            "INSERT INTO user_personas (name, description, avatar_color, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                data["name"],
                data.get("description", ""),
                data.get("avatar_color"),
                now,
                now,
            ),
        )
        persona_id = cur.lastrowid
        assert persona_id is not None
        await db.commit()
        result = await get_user_persona(persona_id)
        assert result is not None
        return result


async def update_user_persona(persona_id: int, data: dict) -> UserPersonaRow | None:
    async with get_db() as db:
        allowed = ["name", "description", "avatar_color"]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            sets.append("updated_at = ?")
            vals.append(datetime.now(timezone.utc).isoformat())
            vals.append(persona_id)
            await db.execute(
                f"UPDATE user_personas SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            await db.commit()
        return await get_user_persona(persona_id)


async def delete_user_persona(persona_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM user_personas WHERE id = ?", (persona_id,))
        await db.commit()
        return cur.rowcount > 0
