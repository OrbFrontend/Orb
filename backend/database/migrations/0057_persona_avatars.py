"""
0057_persona_avatars -- give user personas an image, and add the chat-avatar
gutter toggle.

``user_personas`` previously stored only ``avatar_color``; the two new columns
hold the same base64/mime pair ``character_cards`` uses. ``show_chat_avatars``
defaults to 0 so an upgrading install looks exactly as it did: no persona has an
image yet, and the message gutter stays off until the user asks for it.

Both tables are guarded on existence, not just on their columns: a database
restored from a partial dump reaches the chain without every table, and an
unguarded ALTER would abort the run for everything after it.
"""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """The table's column names, or an empty set when it does not exist."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- literal table names


def migrate(conn: sqlite3.Connection) -> None:
    persona_cols = _columns(conn, "user_personas")
    if persona_cols:
        if "avatar_b64" not in persona_cols:
            conn.execute("ALTER TABLE user_personas ADD COLUMN avatar_b64 TEXT DEFAULT NULL")
            print("[migrations] 0057: added avatar_b64 column to user_personas")
        if "avatar_mime" not in persona_cols:
            conn.execute("ALTER TABLE user_personas ADD COLUMN avatar_mime TEXT DEFAULT NULL")
            print("[migrations] 0057: added avatar_mime column to user_personas")

    settings_cols = _columns(conn, "settings")
    if settings_cols and "show_chat_avatars" not in settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN show_chat_avatars INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0057: added show_chat_avatars column to settings")
