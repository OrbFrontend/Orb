"""Add the optional Judge gate to post-processing fragments.

Existing rows get the empty gate, so every fragment keeps running each turn as
before. A rerun finds the column and changes nothing.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if not columns or "post_processing_gate" in columns:
        return
    conn.execute("ALTER TABLE interactive_fragments ADD COLUMN post_processing_gate TEXT NOT NULL DEFAULT ''")
    conn.commit()
    print("[migrations] 0072: added interactive_fragments.post_processing_gate")
