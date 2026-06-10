"""
0026_persona_locking — add the persona_lock_id mirror column to the
conversations and character_cards tables for databases created before
persona locking existed.

A locked persona overrides the global settings.active_persona_id within a
scope: a conversation lock binds to a single conversation, a character lock
binds to a character card (and thus all conversations using it). Each column
is a plain INTEGER pointing at user_personas(id); resolution priority is
conversation lock → character lock → global active persona.

NOTE: an ALTER-added column cannot carry a REFERENCES clause whose ON DELETE
action is reliably enforced on already-migrated SQLite databases, so the FK
action is omitted here. Dangling locks are cleared explicitly in
delete_user_persona() instead of relying on ON DELETE SET NULL.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    for table in ("conversations", "character_cards"):
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "persona_lock_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN persona_lock_id INTEGER")
            print(f"[migrations] 0026: added persona_lock_id column to {table}")
