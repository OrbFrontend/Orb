"""Store the reference clip a character's cloned voice was enrolled from.

One row per card at most: a character has one voice. The clip is the exact
six-second window the speaker encoder read, not the file the user picked — that
file can be ten minutes of a podcast, and only the first six seconds affect the
enrolled identity, so keeping the rest would be storing something the feature
never looked at.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from ..connection import get_db


async def set_character_voice_ref(card_id: str, audio: bytes, mime: str, source_name: str) -> None:
    """Replace a card's stored reference clip."""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO character_voice_refs (character_card_id, data_b64, mime, source_name, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(character_card_id) DO UPDATE SET "
            "data_b64 = excluded.data_b64, mime = excluded.mime, "
            "source_name = excluded.source_name, created_at = excluded.created_at",
            (
                card_id,
                base64.b64encode(audio).decode("ascii"),
                mime,
                source_name,
                datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()


async def get_character_voice_ref(card_id: str) -> tuple[bytes, str] | None:
    """``(audio_bytes, mime)`` for a card's reference clip, or ``None``."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT data_b64, mime FROM character_voice_refs WHERE character_card_id = ?",
                (card_id,),
            )
        )
        if not rows:
            return None
        return base64.b64decode(rows[0]["data_b64"]), rows[0]["mime"]


async def get_character_voice_ref_info(card_id: str) -> dict | None:
    """The clip's metadata WITHOUT its bytes, or ``None``.

    Separate from the fetch so the config panel can say "enrolled from
    memo.m4a" without moving 192 KB of base64 through a JSON response that is
    rendered as one line of text.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT mime, source_name, created_at, length(data_b64) AS size FROM character_voice_refs "
                "WHERE character_card_id = ?",
                (card_id,),
            )
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "mime": row["mime"],
            "source_name": row["source_name"],
            "created_at": row["created_at"],
            "size_bytes": int(row["size"] or 0) // 4 * 3,  # base64 expands 3 bytes to 4
        }


async def delete_character_voice_ref(card_id: str) -> bool:
    """Remove a card's reference clip. Returns whether a row was deleted."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM character_voice_refs WHERE character_card_id = ?", (card_id,))
        await db.commit()
        return bool(cursor.rowcount)
