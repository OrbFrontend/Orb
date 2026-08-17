"""0055_group_lineage -- group families, so a fork stops reading as a new group.

Before this, every ``kind='group'`` conversation was a group as far as the
sidebar was concerned, and Checkpoint / Compress History both produce one. A
checkpoint therefore duplicated the group instead of branching it.

``group_root_id`` names the conversation a family descends from. NULL means the
row *is* that root, which is why nothing is backfilled here: every conversation
that exists at migration time is the origin of its own family, and that is
exactly what NULL already says. Only forks taken from here on carry a value.
"""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 — literal table name


def migrate(conn: sqlite3.Connection) -> None:
    if "group_root_id" not in _columns(conn, "conversations"):
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN group_root_id TEXT DEFAULT NULL "
            "REFERENCES conversations(id) ON DELETE SET NULL"
        )
        print("[migrations] 0055: added group_root_id column to conversations")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_group_root ON conversations(group_root_id)")
