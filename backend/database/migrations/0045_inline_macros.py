"""Add macro_choices for persistent fragment random picks and macro_seed for
rebuilt prompt fields.

Empty seed uses conversation id; forks copy the effective seed for prefix parity.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    director_cols = {row[1] for row in conn.execute("PRAGMA table_info(director_state)").fetchall()}
    if "macro_choices" not in director_cols:
        conn.execute("ALTER TABLE director_state ADD COLUMN macro_choices TEXT NOT NULL DEFAULT '{}'")
        print("[migrations] 0044: added macro_choices column to director_state")
    conv_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "macro_seed" not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN macro_seed TEXT NOT NULL DEFAULT ''")
        print("[migrations] 0044: added macro_seed column to conversations")
    conn.commit()
