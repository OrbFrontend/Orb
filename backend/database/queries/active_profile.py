"""Which profile is active: the one every per-profile query reads when it is given none."""

from __future__ import annotations

import aiosqlite

from ...core.prompt_layout import DEFAULT_LAYOUT_ID
from ..connection import get_db

# Profiles in picker order, 'default' first; binds DEFAULT_LAYOUT_ID.
PROFILE_ORDER = "ORDER BY id != ?, name COLLATE NOCASE"


async def active_profile_id(db: aiosqlite.Connection) -> str:
    """The active profile over an open connection; the first one when the setting names none that exists."""
    rows = list(
        await db.execute_fetchall("SELECT p.id FROM settings s JOIN profiles p ON p.id = s.active_profile_id WHERE s.id = 1")
    )
    if not rows:
        rows = list(await db.execute_fetchall(f"SELECT id FROM profiles {PROFILE_ORDER} LIMIT 1", (DEFAULT_LAYOUT_ID,)))  # nosec B608
    return str(rows[0][0]) if rows else DEFAULT_LAYOUT_ID


async def get_active_profile_id() -> str:
    """The active profile; the first one when the setting names none that exists."""
    async with get_db() as db:
        return await active_profile_id(db)
