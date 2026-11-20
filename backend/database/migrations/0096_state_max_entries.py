"""Add a per-fragment entry limit to state fragments.

The starter Inventory and Characterization lists keep the 12 they were held to; every other state fragment gets no limit. A
rerun changes nothing.
"""

from __future__ import annotations

import sqlite3

# A snapshot of seeds.STARTER_STATE_MAX_ENTRIES and the starter list ids; later seed changes do not rewrite existing rows.
_STARTER_LIMIT = 12
_STARTER_IDS = ("inventory", "characterization")


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if not columns:
        return
    if "state_max_entries" not in columns:
        conn.execute(
            "ALTER TABLE interactive_fragments ADD COLUMN state_max_entries INTEGER DEFAULT NULL "
            "CHECK (state_max_entries IS NULL OR state_max_entries >= 0)"
        )
    starters = conn.execute(
        "UPDATE interactive_fragments SET state_max_entries = ? "
        "WHERE field_type = 'state' AND state_max_entries IS NULL AND id IN (?, ?)",
        (_STARTER_LIMIT, *_STARTER_IDS),
    )
    others = conn.execute(
        "UPDATE interactive_fragments SET state_max_entries = 0 WHERE field_type = 'state' AND state_max_entries IS NULL"
    )
    conn.commit()
    print(f"[migrations] 0096: state entry limits set ({starters.rowcount} starter lists at 12, {others.rowcount} unlimited)")
