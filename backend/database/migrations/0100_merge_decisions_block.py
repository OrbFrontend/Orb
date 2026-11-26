"""Drop the prompt layouts' Decisions block: the Judge's Writer rulings now ride Scene guidance at their list places.

Each layout keeps its Scene guidance block as it was, switch included. A rerun changes nothing.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'prompt_blocks'").fetchone():
        return
    dropped = conn.execute("DELETE FROM prompt_blocks WHERE kind = 'decisions'")
    conn.commit()
    print(f"[migrations] 0100: {dropped.rowcount} Decisions blocks dropped; Scene guidance carries the rulings")
