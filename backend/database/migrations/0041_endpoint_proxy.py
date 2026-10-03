"""Add per-endpoint proxy_url; empty means direct. Validate schemes at the API layer."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(endpoints)").fetchall()}
    if "proxy" not in cols:
        conn.execute("ALTER TABLE endpoints ADD COLUMN proxy TEXT NOT NULL DEFAULT ''")
        print("[migrations] 0041: added proxy column to endpoints")
