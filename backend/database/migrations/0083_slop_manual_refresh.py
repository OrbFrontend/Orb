"""Drop slop_mining_state.replies_at_run: suggestions refresh only on request, so nothing reads the reply count."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(slop_mining_state)").fetchall()}
    if "replies_at_run" in cols:
        conn.execute("ALTER TABLE slop_mining_state DROP COLUMN replies_at_run")
