"""Migration 0065: add per-fragment cooldown configuration and message snapshots."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    mood_cols = {row[1] for row in conn.execute("PRAGMA table_info(mood_fragments)").fetchall()}
    if mood_cols and "cooldown_turns" not in mood_cols:
        conn.execute("ALTER TABLE mood_fragments ADD COLUMN cooldown_turns INTEGER NOT NULL DEFAULT 0")

    interactive_cols = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if interactive_cols and "cooldown_turns" not in interactive_cols:
        conn.execute("ALTER TABLE interactive_fragments ADD COLUMN cooldown_turns INTEGER NOT NULL DEFAULT 0")

    message_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if message_cols and "fragment_cooldowns" not in message_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN fragment_cooldowns TEXT NOT NULL DEFAULT '{}'")

    conn.commit()
    print("[migrations] 0065: added fragment cooldown columns")
