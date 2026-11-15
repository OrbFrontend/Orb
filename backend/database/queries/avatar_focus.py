"""Store where each card's avatar thumbnail crops."""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..connection import get_db, select_rows
from ..models import AvatarVersionRow

AVATAR_FOCUS_MANUAL = "manual"


def _encode(focus: Sequence[float] | None) -> str:
    return json.dumps([round(float(v), 4) for v in focus]) if focus else ""


async def get_avatar_version(card_id: str, detector: str) -> AvatarVersionRow | None:
    """The avatar's version and stored focus, and whether the *detector* local-ML feature is on; None without an avatar.

    One read answers a revalidation: the NULL test reads only the record header, and the switch rides the same statement.
    """
    rows = await select_rows(
        "SELECT c.updated_at, f.focus, f.stamp, "
        "COALESCE((SELECT json_extract(local_ml_enabled, ?) FROM settings WHERE id = 1), 1) != 0 AS detector_enabled "
        "FROM character_cards c LEFT JOIN character_avatar_focus f ON f.character_card_id = c.id "
        "WHERE c.id = ? AND c.avatar_b64 IS NOT NULL",
        (f"$.{detector}", card_id),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "updated_at": str(row["updated_at"]),
        "focus": json.loads(row["focus"]) if row["focus"] else None,
        "focus_stamp": row["stamp"] or "",
        "detector_enabled": int(row["detector_enabled"]),
    }


async def apply_detected_avatar_focus(card_id: str, focus: Sequence[float] | None, stamp: str, updated_at: str) -> None:
    """Store a detected focus unless the card changed since it was read or the user set one."""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO character_avatar_focus (character_card_id, focus, stamp) "
            "SELECT id, ?, ? FROM character_cards WHERE id = ? AND updated_at = ? "
            "ON CONFLICT(character_card_id) DO UPDATE SET focus = excluded.focus, stamp = excluded.stamp "
            "WHERE character_avatar_focus.stamp != ?",
            (_encode(focus), stamp, card_id, updated_at, AVATAR_FOCUS_MANUAL),
        )
        await db.commit()


async def set_manual_avatar_focus(card_id: str, focus: Sequence[float]) -> bool:
    """Store the user's focus; False when the card has no avatar."""
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO character_avatar_focus (character_card_id, focus, stamp) "
            "SELECT id, ?, ? FROM character_cards WHERE id = ? AND avatar_b64 IS NOT NULL "
            "ON CONFLICT(character_card_id) DO UPDATE SET focus = excluded.focus, stamp = excluded.stamp",
            (_encode(focus), AVATAR_FOCUS_MANUAL, card_id),
        )
        await db.commit()
    return cursor.rowcount == 1


async def clear_avatar_focus(card_id: str) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM character_avatar_focus WHERE character_card_id = ?", (card_id,))
        await db.commit()
