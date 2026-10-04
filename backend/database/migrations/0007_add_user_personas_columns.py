"""0007_add_user_personas_columns — add avatar_color and updated_at columns to user_personas table for databases that were
created before these columns were added.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    # Check if the columns already exist (safety for manual runs)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(user_personas)").fetchall()]

    add_columns(conn, "user_personas", "avatar_color TEXT", migration="0007")

    if "updated_at" not in columns:
        now = datetime.now(UTC).isoformat()
        conn.execute("ALTER TABLE user_personas ADD COLUMN updated_at TEXT")
        # Backfill existing rows with current timestamp
        conn.execute("UPDATE user_personas SET updated_at = ? WHERE updated_at IS NULL", (now,))
        print("[migrations] 0007: added updated_at column to user_personas")
