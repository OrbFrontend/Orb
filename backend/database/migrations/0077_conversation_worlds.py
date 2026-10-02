"""Move World activation from navigation to conversation settings."""

import sqlite3

from backend.database.schema import table_create_sql


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(worlds)")}
    if "enabled" in columns:
        conn.commit()
        had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN")
            conn.execute(table_create_sql("worlds").replace("CREATE TABLE IF NOT EXISTS worlds", "CREATE TABLE worlds_new", 1))
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
