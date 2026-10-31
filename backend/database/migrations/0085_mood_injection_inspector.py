"""Add the Inspector's mood-free injection block and its show_mood_injection setting."""

from __future__ import annotations

import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    if not conn.execute("PRAGMA table_info(settings)").fetchall():
        # No tables yet: init_db creates them from schema.py with these columns already on them.
        return
    add_columns(conn, "settings", "show_mood_injection INTEGER NOT NULL DEFAULT 1", migration="0085")
    add_columns(conn, "conversation_logs", "injection_block_without_moods TEXT", migration="0085")
    conn.commit()
