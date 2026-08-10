"""
0053_dynamic_worlds -- Agent-managed dynamic overlay on top of authored lore.

Adds the two World-level columns the overlay needs (`dynamic_enabled`, the
per-World opt-in; `content_revision`, the optimistic-concurrency stamp bumped
once per lore-content mutation), the five `lorebook_entries` columns that
distinguish an authored row from an Agent-owned overlay row, and the
`world_changesets` table holding proposals and applied history.

Every pre-existing entry backfills as `entry_layer='authored'` with an empty
`overlay_action`, so the projection sees exactly the rows it saw before this
migration. `content_revision` starts at 0 for every World: it is a comparison
token, not a count, so it needs no historical reconstruction.
"""

from __future__ import annotations

import sqlite3

_WORLD_COLUMNS = (
    ("dynamic_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("content_revision", "INTEGER NOT NULL DEFAULT 0"),
)

# `entry_layer` / `overlay_action` are added without their CHECK constraints:
# SQLite's ALTER TABLE ADD COLUMN cannot express one. Fresh installs get them
# from schema.py; on an upgraded DB the writers are the same code paths, so the
# constraint is enforced in Python either way. (The schema-equivalence gate
# compares fresh installs, which do carry the CHECKs.)
_ENTRY_COLUMNS = (
    ("entry_layer", "TEXT NOT NULL DEFAULT 'authored'"),
    ("entry_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("overlay_action", "TEXT NOT NULL DEFAULT ''"),
    ("supersedes_entry_id", "INTEGER DEFAULT NULL REFERENCES lorebook_entries(id) ON DELETE CASCADE"),
    ("archived", "INTEGER NOT NULL DEFAULT 0"),
)

_CHANGESETS_SQL = """
CREATE TABLE IF NOT EXISTS world_changesets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'rejected', 'stale', 'reverted')),
    base_revision INTEGER NOT NULL DEFAULT 0,
    applied_revision INTEGER DEFAULT NULL,
    source_user_message_id INTEGER DEFAULT NULL REFERENCES messages(id) ON DELETE SET NULL,
    source_assistant_message_id INTEGER DEFAULT NULL REFERENCES messages(id) ON DELETE SET NULL,
    source_conversation_id TEXT DEFAULT NULL REFERENCES conversations(id) ON DELETE SET NULL,
    source_character_label TEXT NOT NULL DEFAULT '',
    source_conversation_label TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'agent'
        CHECK (origin IN ('agent', 'undo', 'reset', 're_evaluate')),
    summary TEXT NOT NULL DEFAULT '',
    operations TEXT NOT NULL DEFAULT '[]',
    before_entries TEXT NOT NULL DEFAULT '[]',
    after_entries TEXT NOT NULL DEFAULT '[]',
    reverts_changeset_id INTEGER DEFAULT NULL REFERENCES world_changesets(id) ON DELETE SET NULL,
    supersedes_changeset_id INTEGER DEFAULT NULL REFERENCES world_changesets(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT DEFAULT NULL,
    applied_at TEXT DEFAULT NULL
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_lorebook_overlay ON lorebook_entries(world_id, entry_layer, archived)",
    "CREATE INDEX IF NOT EXISTS idx_changeset_world_status ON world_changesets(world_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_changeset_source_asst ON world_changesets(source_assistant_message_id)",
)


def _add_columns(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 — literal table names
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")  # nosec B608 — literal names from a module constant
            print(f"[migrations] 0053: added {name} column to {table}")


def migrate(conn: sqlite3.Connection) -> None:
    _add_columns(conn, "worlds", _WORLD_COLUMNS)
    _add_columns(conn, "lorebook_entries", _ENTRY_COLUMNS)
    # Belt-and-braces for a DB whose column default was applied before this
    # backfill ran: every pre-existing row is authored by definition.
    conn.execute("UPDATE lorebook_entries SET entry_layer = 'authored' WHERE entry_layer IS NULL OR entry_layer = ''")
    conn.execute("UPDATE lorebook_entries SET overlay_action = '' WHERE overlay_action IS NULL")
    conn.execute("UPDATE lorebook_entries SET archived = 0 WHERE archived IS NULL")
    conn.execute(_CHANGESETS_SQL)
    for sql in _INDEXES:
        conn.execute(sql)
