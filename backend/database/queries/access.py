"""The access password row."""

from __future__ import annotations

from typing import cast

from .. import connection
from ..connection import get_db, select_rows
from ..models import AccessPasswordRow

_cached: tuple[str, AccessPasswordRow | None] | None = None


async def get_access_password() -> AccessPasswordRow | None:
    global _cached
    path = connection.DB_PATH
    if _cached is not None and _cached[0] == path:
        return _cached[1]
    rows = await select_rows("SELECT password_hash, session_key FROM access_password WHERE id = 1")
    row = cast(AccessPasswordRow, dict(rows[0])) if rows else None
    _cached = (path, row)
    return row


async def set_access_password(row: AccessPasswordRow) -> None:
    global _cached
    async with get_db() as db:
        await db.execute(
            "INSERT INTO access_password (id, password_hash, session_key) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash, session_key = excluded.session_key",
            (row["password_hash"], row["session_key"]),
        )
        await db.commit()
    _cached = (connection.DB_PATH, row)


async def clear_access_password() -> None:
    global _cached
    async with get_db() as db:
        await db.execute("DELETE FROM access_password")
        await db.commit()
    _cached = (connection.DB_PATH, None)
