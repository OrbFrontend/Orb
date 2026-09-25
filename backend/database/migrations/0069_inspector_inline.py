"""Add the setting that shows each reply's Inspector sections in the chat."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if cols and "inspector_inline" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN inspector_inline INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0069: added inspector_inline column to settings")
