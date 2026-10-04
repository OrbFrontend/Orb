"""Add direction-note storage and settings, defaulting recording/injection off and fragment timing to post_turn.

Freeze historical DDL here: 0067 converts notes and 0079 drops the table.
"""

from __future__ import annotations

import sqlite3

from .helpers import add_columns

_DIRECTION_NOTES_SQL = """
CREATE TABLE IF NOT EXISTS direction_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    interactive_fragment_id TEXT NOT NULL DEFAULT '',
    interactive_fragment_label TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(_DIRECTION_NOTES_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dirnote_message ON direction_notes(message_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dirnote_conversation ON direction_notes(conversation_id)")

    add_columns(
        conn,
        "settings",
        "direction_notes_record INTEGER NOT NULL DEFAULT 0",
        "direction_notes_inject TEXT NOT NULL DEFAULT 'off'",
        migration="0035",
    )

    add_columns(conn, "interactive_fragments", "direction_note_timing TEXT NOT NULL DEFAULT 'post_turn'", migration="0035")

    # Ship the default direction_note fragment to existing installs; the guard makes this a
    # no-op on fresh ones, which seeded it before migrations ran. Keep this description in sync
    # with the 'characterization' entry in SEED_INTERACTIVE_FRAGMENTS so the two never diverge.
    frag_ids = {row[0] for row in conn.execute("SELECT id FROM interactive_fragments").fetchall()}
    if "characterization" not in frag_ids:
        conn.execute(
            "INSERT INTO interactive_fragments "
            "(id, label, description, field_type, required, enabled, injection_label, sort_order, direction_note_timing) "
            "VALUES ('characterization', 'Characterization', ?, 'direction_note', 0, 0, 'Characterization', 6, 'post_turn')",
            (
                "Record the substantial, long-lasting changes this turn's events have caused in a "
                "character's established characterization -- how they act, how they relate to the user "
                "and other characters, or what they believe about the world or themselves. A single "
                "character may change in several of these ways at once; record every change that "
                "qualifies -- several, one, or none at all -- naming the character, the change, and its cause.",
            ),
        )
        print("[migrations] 0035: seeded the default 'characterization' direction_note fragment")
