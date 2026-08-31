"""Add Dynamic Worlds columns, overlay fields, and changesets."""

from __future__ import annotations

import sqlite3

from backend.database import schema

_WORLD_COLUMNS = (
    ("dynamic_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("content_revision", "INTEGER NOT NULL DEFAULT 0"),
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_lorebook_overlay ON lorebook_entries(world_id, entry_layer, archived)",
    "CREATE INDEX IF NOT EXISTS idx_changeset_world_status ON world_changesets(world_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_changeset_source_asst ON world_changesets(source_assistant_message_id)",
)


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]  # nosec B608 — literal table names


def _rebuild(conn: sqlite3.Connection, table: str) -> None:
    """Replace a table with its canonical schema and copy its rows."""
    conn.commit()
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        block = schema.table_create_sql(table)
        conn.execute(block.replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {table}_new", 1))
        # Intersect old and new columns rather than assuming they match: the old
        # table predates every column this migration is here to introduce.
        old = set(_column_names(conn, table))
        cols = ", ".join(c for c in _column_names(conn, f"{table}_new") if c in old)
        conn.execute(f"INSERT INTO {table}_new ({cols}) SELECT {cols} FROM {table}")  # nosec B608 — names from the canonical schema
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        conn.commit()
    finally:
        if had_fk:
            conn.execute("PRAGMA foreign_keys=ON")


def migrate(conn: sqlite3.Connection) -> None:
    existing = set(_column_names(conn, "worlds"))
    for name, ddl in _WORLD_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE worlds ADD COLUMN {name} {ddl}")  # nosec B608 — literal names from a module constant
            print(f"[migrations] 0053: added {name} column to worlds")

    if "entry_layer" not in set(_column_names(conn, "lorebook_entries")):
        _rebuild(conn, "lorebook_entries")
        print("[migrations] 0053: lorebook_entries rebuilt with the Agent-overlay columns")

    conn.execute(schema.table_create_sql("world_changesets"))
    for sql in _INDEXES:
        conn.execute(sql)
