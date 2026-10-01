"""Add the expression playback rendering preference; retain Classic by default."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
    if cols and "expression_rendering" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN expression_rendering TEXT NOT NULL DEFAULT 'classic'")
