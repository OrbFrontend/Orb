"""Drop the progressive-field and direction-note storage that 0067 converted.

0067 moved this data to ``fragment_state_events`` and state fragments, and nothing has read it since. Shared card files still
carry the old fragment types; those are mapped at the card read boundary and never touched these columns. A rerun finds nothing
left to drop and changes nothing.
"""

from __future__ import annotations

import sqlite3

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("settings", "direction_notes_record"),
    ("settings", "direction_notes_inject"),
    ("interactive_fragments", "direction_note_timing"),
    ("messages", "progressive_fields"),
    ("director_state", "progressive_fields"),
)


def migrate(conn: sqlite3.Connection) -> None:
    dropped: list[str] = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'direction_notes'").fetchone():
        conn.execute("DROP TABLE direction_notes")
        dropped.append("direction_notes")
    for table, column in _COLUMNS:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")  # nosec B608 -- module literals
            dropped.append(f"{table}.{column}")
    conn.commit()
    if dropped:
        print(f"[migrations] 0079: dropped {', '.join(dropped)}")
