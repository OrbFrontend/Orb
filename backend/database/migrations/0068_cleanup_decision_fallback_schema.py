"""Remove the retired decision fallback column and restore the endpoint FK."""

from __future__ import annotations

import sqlite3

from backend.database import schema


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- literal table names


def _has_decision_endpoint_fk(conn: sqlite3.Connection) -> bool:
    return any(
        row[2] == "endpoints" and row[3] == "decision_endpoint_id" and row[4] == "id" and row[6].upper() == "SET NULL"
        for row in conn.execute("PRAGMA foreign_key_list(settings)").fetchall()
    )


def _rebuild_settings(conn: sqlite3.Connection) -> None:
    """Rebuild settings from the canonical DDL so the decision endpoint is an FK."""
    block = schema.table_create_sql("settings")
    conn.execute(block.replace("CREATE TABLE IF NOT EXISTS settings", "CREATE TABLE settings_0068_new", 1))
    new_columns = _columns(conn, "settings_0068_new")
    old_columns = _columns(conn, "settings")
    columns = ", ".join(column for column in new_columns if column in old_columns)
    conn.execute(
        f"INSERT INTO settings_0068_new ({columns}) SELECT {columns} FROM settings"  # nosec B608 -- names from canonical DDL
    )
    conn.execute("DROP TABLE settings")
    conn.execute("ALTER TABLE settings_0068_new RENAME TO settings")


def migrate(conn: sqlite3.Connection) -> None:
    fragment_columns = _columns(conn, "interactive_fragments")
    needs_column_cleanup = "decision_default" in fragment_columns
    needs_settings_rebuild = bool(_columns(conn, "settings")) and not _has_decision_endpoint_fk(conn)
    if not (needs_column_cleanup or needs_settings_rebuild):
        return

    # DROP COLUMN and DROP/RENAME both require foreign-key enforcement to be off
    # during the shape change. The migration runner has committed before calling
    # us, so this pragma is effective here and the prior state is restored on all
    # exit paths.
    conn.commit()
    had_foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        if needs_column_cleanup:
            conn.execute("ALTER TABLE interactive_fragments DROP COLUMN decision_default")
            print("[migrations] 0068: dropped interactive_fragments.decision_default")
        if needs_settings_rebuild:
            _rebuild_settings(conn)
            print("[migrations] 0068: rebuilt settings with the decision endpoint foreign key")
        conn.commit()
    finally:
        conn.execute(f"PRAGMA foreign_keys = {had_foreign_keys}")
