"""Migration 0004: create director_fragments table."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS director_fragments (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            description TEXT NOT NULL,
            field_type TEXT NOT NULL DEFAULT 'string',
            required BOOLEAN NOT NULL DEFAULT 0,
            enabled BOOLEAN NOT NULL DEFAULT 1,
            injection_label TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
