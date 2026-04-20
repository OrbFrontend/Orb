"""Migration 0006: rename fragments table to mood_fragments."""

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    # Check if the old fragments table exists.
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fragments'"
    )
    if cursor.fetchone() is None:
        # No old table — nothing to migrate (already using mood_fragments).
        return

    # Check if mood_fragments already exists.
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mood_fragments'"
    )
    mood_exists = cursor.fetchone() is not None

    if not mood_exists:
        # Fresh migration path: mood_fragments doesn't exist, safe to rename.
        conn.execute("ALTER TABLE fragments RENAME TO mood_fragments")
    else:
        # Re-run path: mood_fragments was created by init_db() seed data.
        # Copy data from fragments into mood_fragments (ignore duplicates by id).
        conn.execute(
            """
            INSERT OR IGNORE INTO mood_fragments
                (id, label, description, prompt_text, negative_prompt, enabled)
            SELECT id, label, description, prompt_text, negative_prompt, enabled
            FROM fragments
            """
        )
        # Drop the old fragments table.
        conn.execute("DROP TABLE fragments")

    conn.commit()
