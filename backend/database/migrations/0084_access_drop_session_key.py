"""Drop access_password.session_key: a sign-in pass lives in memory and opens the page once, so nothing reads the key."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(access_password)").fetchall()}
    if "session_key" in cols:
        conn.execute("ALTER TABLE access_password DROP COLUMN session_key")
