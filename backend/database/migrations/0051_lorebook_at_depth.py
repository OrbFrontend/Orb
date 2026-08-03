"""
0051_lorebook_at_depth -- `@ Depth` placement for constant entries.

A constant entry with `at_depth` rides the per-turn tail (after the user
message, macros re-resolved every turn) instead of the cached system prefix.
Defaults to 0, so existing entries keep their prefix placement.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(lorebook_entries)").fetchall()}
    if "at_depth" not in cols:
        conn.execute("ALTER TABLE lorebook_entries ADD COLUMN at_depth INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0051: added at_depth column to lorebook_entries")
