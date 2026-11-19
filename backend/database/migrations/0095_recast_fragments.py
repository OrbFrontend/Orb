"""Rename post-processing fragments to recast fragments.

The ``post_processing`` field type becomes ``recast`` and its gate columns become ``recast_gate`` and ``recast_gate_replies``.
Card files keep the old names; they are mapped at the card read boundary. A rerun finds the new names and changes nothing.
"""

from __future__ import annotations

import sqlite3

_RENAMES = (("post_processing_gate", "recast_gate"), ("post_processing_gate_replies", "recast_gate_replies"))


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if not columns:
        return
    for old, new in _RENAMES:
        if old in columns and new not in columns:
            conn.execute(f"ALTER TABLE interactive_fragments RENAME COLUMN {old} TO {new}")  # nosec B608 -- literals
    renamed = conn.execute("UPDATE interactive_fragments SET field_type = 'recast' WHERE field_type = 'post_processing'")
    conn.commit()
    print(f"[migrations] 0095: post-processing fragments are recast fragments ({renamed.rowcount} rows)")
