"""Idempotent DDL shared by historical migrations; callers supply literal names."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable


def add_columns(conn: sqlite3.Connection, table: str, *declarations: str, migration: str | None = None) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608 -- migration literals
    for declaration in declarations:
        name = declaration.split()[0]
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")  # nosec B608 -- migration literals
            columns.add(name)
            if migration:
                print(f"[migrations] {migration}: added {name} column to {table}")


def column_migration(table: str, *declarations: str, migration: str | None = None) -> Callable[[sqlite3.Connection], None]:
    def migrate(conn: sqlite3.Connection) -> None:
        add_columns(conn, table, *declarations, migration=migration)

    return migrate
