"""
0050_lorebook_v3_fields -- Character Card V3 lorebook entry semantics.

`use_regex` (keys are regular expressions), `selective` + `secondary_keys`
(a secondary key must also match). Parsed on import before this, then dropped.
"""

from __future__ import annotations

import sqlite3

_COLUMNS = {
    "use_regex": "INTEGER NOT NULL DEFAULT 0",
    "selective": "INTEGER NOT NULL DEFAULT 0",
    "secondary_keys": "TEXT NOT NULL DEFAULT '[]'",
}


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(lorebook_entries)").fetchall()}
    for name, decl in _COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE lorebook_entries ADD COLUMN {name} {decl}")
            print(f"[migrations] 0050: added {name} column to lorebook_entries")
