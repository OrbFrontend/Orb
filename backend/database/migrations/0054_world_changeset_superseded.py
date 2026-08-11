"""0054_world_changeset_superseded -- add the terminal re-evaluation state.

Early Dynamic Worlds builds shipped migration 0053 before re-evaluation gained
an atomic terminal state. SQLite cannot extend a CHECK constraint in place, so
databases that already ran that build need the table rebuilt from the canonical
fresh-install DDL. A database whose 0053 already contains ``superseded`` is left
alone.
"""

from __future__ import annotations

import sqlite3

from backend.database import schema

_TABLE = "world_changesets"
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_changeset_world_status ON world_changesets(world_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_changeset_source_asst ON world_changesets(source_assistant_message_id)",
)


def _already_current(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_TABLE,),
    ).fetchone()
    return row is None or "'superseded'" in (row[0] or "")


def _rebuild(conn: sqlite3.Connection) -> None:
    block = schema.table_create_sql(_TABLE)
    new_ddl = block.replace(f"CREATE TABLE IF NOT EXISTS {_TABLE}", f"CREATE TABLE {_TABLE}_new", 1)
    conn.execute(new_ddl)
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()]
    names = ", ".join(columns)
    conn.execute(
        f"INSERT INTO {_TABLE}_new ({names}) SELECT {names} FROM {_TABLE}"  # nosec B608 — table/columns come from canonical schema
    )
    conn.execute(f"DROP TABLE {_TABLE}")
    conn.execute(f"ALTER TABLE {_TABLE}_new RENAME TO {_TABLE}")
    for statement in _INDEXES:
        conn.execute(statement)


def migrate(conn: sqlite3.Connection) -> None:
    if _already_current(conn):
        return

    # Standard SQLite table-rebuild recipe. The runner commits before invoking
    # us; disabling FK enforcement prevents DROP TABLE from cascading through
    # the message/world pointers while all rows are copied into the replacement.
    conn.commit()
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        _rebuild(conn)
        conn.commit()
        print("[migrations] 0054: added world_changesets superseded status")
    finally:
        if had_fk:
            conn.execute("PRAGMA foreign_keys=ON")
