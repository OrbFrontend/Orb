"""Add how many previous replies a post-processing gate shows the Judge.

Existing rows get 0, so every gate keeps judging the draft alone as before. A
rerun finds the column and changes nothing.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if not columns or "post_processing_gate_replies" in columns:
        return
    conn.execute("ALTER TABLE interactive_fragments ADD COLUMN post_processing_gate_replies INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    print("[migrations] 0078: added interactive_fragments.post_processing_gate_replies")
