"""Per-character expression images (go-emotions label -> image), stored base64
in the ``character_expressions`` table. Replace-all semantics on upload."""

from __future__ import annotations

import base64

from ..connection import get_db


async def set_character_expressions(card_id: str, images: dict[str, tuple[str, str]]) -> None:
    """Replace all of a card's expressions. ``images`` maps label -> (data_b64, mime)."""
    async with get_db() as db:
        await db.execute("DELETE FROM character_expressions WHERE character_card_id = ?", (card_id,))
        await db.executemany(
            "INSERT INTO character_expressions (character_card_id, label, data_b64, mime) VALUES (?, ?, ?, ?)",
            [(card_id, label, b64, mime) for label, (b64, mime) in images.items()],
        )
        await db.commit()


async def list_expression_labels(card_id: str) -> list[str]:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT label FROM character_expressions WHERE character_card_id = ? ORDER BY label",
                (card_id,),
            )
        )
        return [r["label"] for r in rows]


async def get_character_expression(card_id: str, label: str) -> tuple[bytes, str] | None:
    """Returns (image_bytes, mime) or None."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT data_b64, mime FROM character_expressions WHERE character_card_id = ? AND label = ?",
                (card_id, label),
            )
        )
        if not rows:
            return None
        return base64.b64decode(rows[0]["data_b64"]), rows[0]["mime"]


async def delete_character_expressions(card_id: str) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM character_expressions WHERE character_card_id = ?", (card_id,))
        await db.commit()
