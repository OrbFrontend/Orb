"""Add inspector_inline and remove state_updates.

Make previously blocked state fragments Manual only before removing the master switch, preserving upgrade behaviour.
"""

from __future__ import annotations

import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if not cols:
        return
    add_columns(conn, "settings", "inspector_inline INTEGER NOT NULL DEFAULT 0", migration="0069")
    if "state_updates" in cols:
        row = conn.execute("SELECT state_updates FROM settings WHERE id = 1").fetchone()
        if row is not None and not row[0]:
            conn.execute(
                "UPDATE interactive_fragments SET state_update = 'manual' "
                "WHERE field_type = 'state' AND COALESCE(state_update, '') != 'manual'"
            )
        conn.execute("ALTER TABLE settings DROP COLUMN state_updates")
        print("[migrations] 0069: dropped state_updates column from settings")
    conn.commit()
