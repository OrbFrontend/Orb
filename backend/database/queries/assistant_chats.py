"""The panel assistant's held conversations, by session id."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..connection import immediate_tx, select_rows


async def get_assistant_chat(session: str) -> dict[str, Any] | None:
    rows = await select_rows("SELECT transcript FROM assistant_chats WHERE session = ?", (session,))
    return json.loads(rows[0]["transcript"]) if rows else None


async def delete_assistant_chat(session: str) -> None:
    async with immediate_tx() as db:
        await db.execute("DELETE FROM assistant_chats WHERE session = ?", (session,))


async def put_assistant_chat(session: str, transcript: dict[str, Any], keep: int) -> None:
    """Store *session*'s conversation and drop all but the *keep* most recently stored."""
    async with immediate_tx() as db:
        await db.execute(
            "INSERT INTO assistant_chats (session, transcript, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session) DO UPDATE SET transcript = excluded.transcript, updated_at = excluded.updated_at",
            (session, json.dumps(transcript), datetime.now(UTC).isoformat()),
        )
        await db.execute(
            "DELETE FROM assistant_chats WHERE session NOT IN "
            "(SELECT session FROM assistant_chats ORDER BY updated_at DESC LIMIT ?)",
            (keep,),
        )
