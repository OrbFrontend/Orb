"""Record the persona each user message was sent as."""

from __future__ import annotations

import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    if not conn.execute("PRAGMA table_info(messages)").fetchall():
        # No tables yet: init_db creates them from schema.py with these columns already on them.
        return
    add_columns(
        conn,
        "messages",
        "persona_id INTEGER DEFAULT NULL REFERENCES user_personas(id) ON DELETE SET NULL",
        "persona_name TEXT DEFAULT NULL",
        migration="0086",
    )
    conn.commit()
