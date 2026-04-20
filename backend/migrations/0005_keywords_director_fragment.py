"""Migration 0005: insert keywords as a director fragment for existing databases."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    # Only needed for DBs created before keywords became a director fragment.
    # Fresh DBs already have it from the seed in init_db().
    row = conn.execute(
        "SELECT id FROM director_fragments WHERE id = 'keywords'"
    ).fetchone()
    if row is not None:
        return

    conn.execute(
        """
        INSERT INTO director_fragments
            (id, label, description, field_type, required, enabled, injection_label, sort_order)
        VALUES (
            'keywords',
            'Keywords',
            'List of nouns (keywords) to remind the important subjects in the roleplay so far. '
            'Keep under 6 items. Extract from the messages and plot summary. '
            'Ignore obvious things like names of the characters.',
            'array',
            1,
            1,
            'Keywords',
            2
        )
        """
    )
    # Shift sort_order of the fragments that follow keywords.
    conn.execute("UPDATE director_fragments SET sort_order = 3 WHERE id = 'next_event'")
    conn.execute(
        "UPDATE director_fragments SET sort_order = 4 WHERE id = 'writing_direction'"
    )
    conn.execute(
        "UPDATE director_fragments SET sort_order = 5 WHERE id = 'detected_repetitions'"
    )
    conn.commit()
