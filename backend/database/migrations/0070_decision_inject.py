"""Add ``decision_inject``: which passes receive a decision's resolved guidance.

Existing decision fragments keep reaching both the Director and the Writer.
"""

from __future__ import annotations

import sqlite3

_DDL = "TEXT DEFAULT NULL CHECK (decision_inject IS NULL OR decision_inject IN ('director', 'writer', 'both'))"


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if not cols:
        return
    if "decision_inject" not in cols:
        conn.execute(f"ALTER TABLE interactive_fragments ADD COLUMN decision_inject {_DDL}")  # nosec B608 -- module literal
        print("[migrations] 0070: added decision_inject column to interactive_fragments")
    conn.execute(
        "UPDATE interactive_fragments SET decision_inject = 'both' WHERE field_type = 'decision' AND decision_inject IS NULL"
    )
    conn.commit()
