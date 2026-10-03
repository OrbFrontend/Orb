"""Add the agentic_lorebook_enabled flag for Director-selected lorebook entries."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "agentic_lorebook_enabled" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN agentic_lorebook_enabled INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0030: added agentic_lorebook_enabled column to settings")
