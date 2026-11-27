"""Add document revisions, conversation-scoped Worlds, and a persistent dataset epoch."""

import sqlite3

# The worlds table as this migration builds it; 0101 later moves is_global to profile_worlds.
_WORLDS_DDL = """CREATE TABLE worlds_new (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    is_global INTEGER NOT NULL DEFAULT 0,
    dynamic_enabled INTEGER NOT NULL DEFAULT 0,
    content_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if columns and "revision" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(worlds)")}
    if "enabled" in columns:
        conn.commit()
        had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN")
            conn.execute(_WORLDS_DDL)
            linked = (
                "id IN (SELECT world_id FROM character_cards)"
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'character_cards'").fetchone()
                else "0"
            )
            names = [row[1] for row in conn.execute("PRAGMA table_info(worlds_new)")]
            values = [f"CASE WHEN {linked} THEN 0 ELSE enabled END" if name == "is_global" else name for name in names]
            conn.execute(f"INSERT INTO worlds_new ({', '.join(names)}) SELECT {', '.join(values)} FROM worlds")  # nosec B608 -- schema-derived identifiers
            conn.execute("DROP TABLE worlds")
            conn.execute("ALTER TABLE worlds_new RENAME TO worlds")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            if had_fk:
                conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_worlds ("
        "conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,"
        "world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,"
        "enabled INTEGER NOT NULL, PRIMARY KEY (conversation_id, world_id))"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS dataset_meta (id INTEGER PRIMARY KEY CHECK (id = 1), epoch TEXT NOT NULL)")
