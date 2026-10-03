"""Add the lifetime generated-character counter.

NULL triggers lazy initialization from assistant messages; successful turns
then increment it, including after restoring older backups.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "generated_chars" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN generated_chars INTEGER DEFAULT NULL")
        conn.commit()
        print("[migrations] 0029: added generated_chars column to settings")
