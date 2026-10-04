"""Add ``decision_inject``: which passes receive a decision's resolved guidance.

Existing decision fragments keep reaching both the Director and the Writer.
"""

from __future__ import annotations

import sqlite3

from .helpers import add_columns

_DDL = "TEXT DEFAULT NULL CHECK (decision_inject IS NULL OR decision_inject IN ('director', 'writer', 'both'))"


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if not cols:
        return
    add_columns(conn, "interactive_fragments", f"decision_inject {_DDL}", migration="0070")
    conn.execute(
        "UPDATE interactive_fragments SET decision_inject = 'both' WHERE field_type = 'decision' AND decision_inject IS NULL"
    )
    conn.commit()
