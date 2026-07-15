"""Migration 0043: add macro_choices to director_state.

Per-conversation {{random}} picks for mood/interactive fragment text — a JSON
map of "surface:fragment_id:occurrence" → chosen option, so a fragment's
random macros resolve once per conversation and stay fixed.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    director_cols = {row[1] for row in conn.execute("PRAGMA table_info(director_state)").fetchall()}
    if "macro_choices" not in director_cols:
        conn.execute("ALTER TABLE director_state ADD COLUMN macro_choices TEXT NOT NULL DEFAULT '{}'")
        conn.commit()
        print("[migrations] 0043: added macro_choices column to director_state")
