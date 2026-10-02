"""Protect document saves with compare-and-swap revisions."""

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if columns and "revision" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
