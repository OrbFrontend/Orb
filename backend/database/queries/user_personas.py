from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any, cast

from ..connection import _build_set_clause, get_db
from ..models import UserPersonaRow

_PERSONA_SELECT = "SELECT id, name, description, avatar_color, avatar_mime, created_at, updated_at FROM user_personas"


def _project(row: Any) -> UserPersonaRow:
    d = dict(row)
    d["has_avatar"] = d["avatar_mime"] is not None
    return cast(UserPersonaRow, d)


async def get_user_personas() -> list[UserPersonaRow]:
    async with get_db() as db:
        rows = list(await db.execute_fetchall(_PERSONA_SELECT + " ORDER BY name ASC"))
        return [_project(r) for r in rows]


async def get_persona_conversation_counts() -> dict[int, int]:
    """Count conversations by the persona each one speaks as today.

    Mirrors ``resolve_persona_id``: conversation pin, then the conversation's
    card pin, then the global active persona. No history of past active
    personas exists, so unpinned conversations count toward the current one.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                """SELECT COALESCE(c.persona_lock_id, card.persona_lock_id,
                                   (SELECT active_persona_id FROM settings WHERE id = 1)) AS persona_id,
                          COUNT(*) AS conversations
                   FROM conversations AS c
                   LEFT JOIN character_cards AS card ON card.id = c.character_card_id
                   GROUP BY persona_id
                   HAVING persona_id IS NOT NULL"""
            )
        )
    return {int(row["persona_id"]): int(row["conversations"]) for row in rows}


async def get_user_persona(persona_id: int) -> UserPersonaRow | None:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                _PERSONA_SELECT + " WHERE id = ?",
                (persona_id,),
            )
        )
        return _project(rows[0]) if rows else None


async def get_persona_avatar(persona_id: int) -> tuple[bytes, str] | None:
    """Return decoded avatar bytes and MIME type, if present."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT avatar_b64, avatar_mime FROM user_personas WHERE id = ?",
                (persona_id,),
            )
        )
        if not rows or not rows[0]["avatar_b64"]:
            return None
        return base64.b64decode(rows[0]["avatar_b64"]), rows[0]["avatar_mime"]


async def create_user_persona(data: dict) -> UserPersonaRow:
    async with get_db() as db:
        now = datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO user_personas (name, description, avatar_color, avatar_b64, avatar_mime, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                data["name"],
                data.get("description", ""),
                data.get("avatar_color"),
                data.get("avatar_b64"),
                data.get("avatar_mime"),
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
        allowed = ["name", "description", "avatar_color", "avatar_b64", "avatar_mime"]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            sets.append("updated_at = ?")
            vals.append(datetime.now(UTC).isoformat())
            vals.append(persona_id)
            await db.execute(
                f"UPDATE user_personas SET {', '.join(sets)} WHERE id = ?",  # nosec B608 — cols from a hardcoded allowlist, values parameterised
                vals,
            )
            await db.commit()
        return await get_user_persona(persona_id)


async def delete_user_persona(persona_id: int) -> bool:
    async with get_db() as db:
        await db.execute("UPDATE conversations  SET persona_lock_id = NULL WHERE persona_lock_id = ?", (persona_id,))
        await db.execute("UPDATE character_cards SET persona_lock_id = NULL WHERE persona_lock_id = ?", (persona_id,))
        cur = await db.execute("DELETE FROM user_personas WHERE id = ?", (persona_id,))
        await db.commit()
        return cur.rowcount > 0
