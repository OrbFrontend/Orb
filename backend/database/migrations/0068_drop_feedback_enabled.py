"""Drop the ``feedback_enabled`` setting.

Feedback fragments are now gated like post-processing fragments: the Agent and
the fragment's own ``enabled`` flag. Fragments the old master toggle kept off
are disabled first, so no feedback step starts running on upgrade.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "feedback_enabled" not in cols:
        return
    row = conn.execute("SELECT feedback_enabled FROM settings WHERE id = 1").fetchone()
    if row is not None and not row[0]:
        conn.execute("UPDATE interactive_fragments SET enabled = 0 WHERE field_type = 'feedback'")
    conn.execute("ALTER TABLE settings DROP COLUMN feedback_enabled")
    conn.commit()
    print("[migrations] 0068: dropped feedback_enabled column from settings")
