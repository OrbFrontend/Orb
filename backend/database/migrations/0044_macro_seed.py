"""Migration 0044: add macro_seed to conversations.

The seed for seeded {{random}} resolution in per-turn-rebuilt prompt fields
(persona, scenario). Empty means "use the conversation's own id"; checkpoint
and compress copies carry the source's effective seed so seeded picks stay
byte-identical to the copied history instead of re-rolling under the new id.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conv_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "macro_seed" not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN macro_seed TEXT NOT NULL DEFAULT ''")
        conn.commit()
        print("[migrations] 0044: added macro_seed column to conversations")
