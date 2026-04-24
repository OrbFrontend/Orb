"""
0011_add_show_editor_diff -- add show_editor_diff column to settings for
databases created before this toggle existed. Default 1 preserves prior
behavior (editor diff highlights visible).
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "show_editor_diff" not in cols:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN show_editor_diff INTEGER NOT NULL DEFAULT 1"
        )
        print("[migrations] 0011: added show_editor_diff column to settings")
