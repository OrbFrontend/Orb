"""
0007_add_user_personas_columns — add avatar_color and updated_at columns
to user_personas table for databases that were created before these columns
were added.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def migrate(conn: sqlite3.Connection) -> None:
    # Check if the columns already exist (safety for manual runs)
    columns = [
        row[1]
        for row in conn.execute("PRAGMA table_info(user_personas)").fetchall()
    ]

    if "avatar_color" not in columns:
        conn.execute("ALTER TABLE user_personas ADD COLUMN avatar_color TEXT")
        print("[migrations] 0007: added avatar_color column to user_personas")

    if "updated_at" not in columns:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("ALTER TABLE user_personas ADD COLUMN updated_at TEXT")
        # Backfill existing rows with current timestamp
        conn.execute("UPDATE user_personas SET updated_at = ? WHERE updated_at IS NULL", (now,))
        print("[migrations] 0007: added updated_at column to user_personas")
