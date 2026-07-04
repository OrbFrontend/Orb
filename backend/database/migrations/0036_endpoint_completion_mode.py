"""
0036_endpoint_completion_mode -- add the per-endpoint transport selector.

'chat' (default) keeps the OpenAI-compatible /chat/completions transport;
'text' switches to llama.cpp's native /apply-template + /completion path.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(endpoints)").fetchall()}
    if "completion_mode" not in cols:
        conn.execute(
            "ALTER TABLE endpoints ADD COLUMN completion_mode TEXT NOT NULL DEFAULT 'chat' "
            "CHECK (completion_mode IN ('chat', 'text'))"
        )
        print("[migrations] 0036: added completion_mode column to endpoints")
