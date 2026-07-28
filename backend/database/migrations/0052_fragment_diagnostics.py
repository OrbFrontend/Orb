"""Migration 0052: persist per-turn contributed-fragment diagnostics."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_logs)").fetchall()}
    if "fragment_diagnostics" in columns:
        return
    conn.execute("ALTER TABLE conversation_logs ADD COLUMN fragment_diagnostics TEXT NOT NULL DEFAULT '[]'")
    conn.commit()
    print("[migrations] 0052: added conversation_logs.fragment_diagnostics")
