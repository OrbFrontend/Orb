"""Add last_accessed_at for sidebar ordering, backfilled from updated_at.
Content changes and conversation access then have separate timestamps.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()]
    if "last_accessed_at" not in columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN last_accessed_at TEXT")
        conn.execute("UPDATE conversations SET last_accessed_at = updated_at")
        print("[migrations] 0032: added last_accessed_at column to conversations")
