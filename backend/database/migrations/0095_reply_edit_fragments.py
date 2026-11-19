"""Rename post-processing fragments to recast fragments, and seed the disabled Trim rewrite fragment.

The ``post_processing`` field type becomes ``recast``. Its gate columns become ``gate_question`` and ``gate_replies``, which
recast and rewrite fragments share. Card files keep the old names; they are mapped at the card read boundary. Trim joins
the end of the list unless a fragment already has its id. A rerun finds the new names and the seed, and changes nothing.
"""

from __future__ import annotations

import sqlite3

_RENAMES = (("post_processing_gate", "gate_question"), ("post_processing_gate_replies", "gate_replies"))
# A snapshot of seeds.TRIM_DESCRIPTION; later changes to the seed do not rewrite existing rows.
_TRIM_DESCRIPTION = (
    "Combine wordy phrases into fewer, more impactful words, and drop filler and loose beats that add nothing to the scene. "
    "Preserve plot facts, characterization, the meaning of every line of dialogue, and markup; do not add new events."
)


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if not columns:
        return
    for old, new in _RENAMES:
        if old in columns and new not in columns:
            conn.execute(f"ALTER TABLE interactive_fragments RENAME COLUMN {old} TO {new}")  # nosec B608 -- literals
    renamed = conn.execute("UPDATE interactive_fragments SET field_type = 'recast' WHERE field_type = 'post_processing'")
    seeded = conn.execute(
        "INSERT INTO interactive_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order) "
        "SELECT 'trim_prose', 'Trim', ?, 'rewrite', 0, 0, 'Trim', "
        "(SELECT COALESCE(MAX(sort_order), 0) + 1 FROM interactive_fragments) "
        "WHERE NOT EXISTS (SELECT 1 FROM interactive_fragments WHERE id = 'trim_prose')",
        (_TRIM_DESCRIPTION,),
    )
    conn.commit()
    print(
        f"[migrations] 0095: post-processing fragments are recast fragments ({renamed.rowcount} rows); "
        f"Trim rewrite fragment seeded: {bool(seeded.rowcount)}"
    )
