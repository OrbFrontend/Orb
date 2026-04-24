"""
0012_hide_streaming_until_baked -- add hide_streaming_until_baked column to
settings for databases created before this toggle existed. Default 0
preserves prior behavior (streaming message visible live).
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "hide_streaming_until_baked" not in cols:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN hide_streaming_until_baked INTEGER NOT NULL DEFAULT 0"
        )
        print("[migrations] 0012: added hide_streaming_until_baked column to settings")
