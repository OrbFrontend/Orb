"""Add the ``inspector_inline`` setting and drop the ``state_updates`` setting.

``inspector_inline`` shows each reply's Inspector sections in the chat.

State fragments are now gated by the Agent and each fragment's own update
timing. Fragments the old ``state_updates`` master switch kept from updating
become Manual only first, so no update call starts running on upgrade.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if not cols:
        return
    if "inspector_inline" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN inspector_inline INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0069: added inspector_inline column to settings")
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
