"""Add persona_lock_id to conversations and character cards.

Resolution is conversation lock, then card lock, then global persona.
These columns omit FK actions; delete_user_persona clears dangling locks.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    for table in ("conversations", "character_cards"):
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "persona_lock_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN persona_lock_id INTEGER")
            print(f"[migrations] 0026: added persona_lock_id column to {table}")
