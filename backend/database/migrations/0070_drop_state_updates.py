"""Drop the ``state_updates`` setting.

State fragments are now gated by the Agent and each fragment's own update
timing. Fragments the old master switch kept from updating become Manual only
first, so no update call starts running on upgrade.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "state_updates" not in cols:
        return
    row = conn.execute("SELECT state_updates FROM settings WHERE id = 1").fetchone()
    if row is not None and not row[0]:
        conn.execute(
            "UPDATE interactive_fragments SET state_update = 'manual' "
            "WHERE field_type = 'state' AND COALESCE(state_update, '') != 'manual'"
        )
    conn.execute("ALTER TABLE settings DROP COLUMN state_updates")
    conn.commit()
    print("[migrations] 0070: dropped state_updates column from settings")
