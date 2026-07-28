"""Migration 0053: the local active-Writer-resolver selection.

Additive and defaulted to 0, so every existing install upgrades with no
resolver selected -- which is the same state a fresh install starts in, and the
state in which the Writer's tool blob is byte-identical to what it was before
this feature existed.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(extension_packages)").fetchall()}
    if "writer_tool_active" in columns:
        return
    conn.execute("ALTER TABLE extension_packages ADD COLUMN writer_tool_active INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    print("[migrations] 0053: added extension_packages.writer_tool_active")
