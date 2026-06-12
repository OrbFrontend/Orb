"""0027_rebuild_persona_lock_fks — give persona_lock_id a real foreign key on
databases migrated through 0026.

0026 added ``persona_lock_id`` to ``conversations`` and ``character_cards`` as a
bare ``INTEGER`` (an ALTER-added column cannot carry an enforced REFERENCES
clause), while fresh installs declare it
``INTEGER REFERENCES user_personas(id) ON DELETE SET NULL``
(see backend/database/schema.py). The preset engine builds its merge/FK model
from the *live* ``PRAGMA foreign_key_list``, so on a migrated DB those columns
were invisible to the FK machinery: the merge copied lock ids verbatim instead
of remapping them through the personas id-map, and an export that drops the
configs domain never SET-NULLed them. This rebuilds the two tables to the
canonical DDL so the live schema matches a fresh install.

Idempotent: a table whose ``persona_lock_id`` edge already exists (every fresh
install, and any DB already through 0027) is skipped. Run with foreign keys
OFF for the duration — the standard SQLite "other kinds of schema changes"
recipe — so dropping the old table neither cascades into ``messages`` nor trips
a constraint. Both tables have TEXT primary keys, so child references
(``messages.conversation_id`` …) keep resolving across the drop/rename.

The rebuilt DDL is derived from ``schema.table_create_sql`` rather than pasted,
so this migration can never disagree with the schema-equivalence gate.
"""

from __future__ import annotations

import sqlite3

from backend.database import schema

_TABLES = ("conversations", "character_cards")


def _has_persona_lock_fk(conn: sqlite3.Connection, table: str) -> bool:
    # PRAGMA foreign_key_list row: (id, seq, parent_table, from, to, on_update, on_delete, match)
    for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        if row[3] == "persona_lock_id" and row[2] == "user_personas":
            return True
    return False


def _rebuild(conn: sqlite3.Connection, table: str) -> None:
    block = schema.table_create_sql(table)
    new_ddl = block.replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {table}_new", 1)
    conn.execute(new_ddl)
    new_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table}_new)").fetchall()]
    old_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    cols = ", ".join(c for c in new_cols if c in old_cols)
    conn.execute(f"INSERT INTO {table}_new ({cols}) SELECT {cols} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    print(f"[migrations] 0027: rebuilt {table} with the persona_lock_id foreign key")


def migrate(conn: sqlite3.Connection) -> None:
    # PRAGMA foreign_keys is a no-op inside a transaction, and DROP/RENAME under
    # FK enforcement could cascade or fail; the runner has committed before this
    # call, so close any stray transaction, flip FKs off for the rebuild, then
    # restore the prior state.
    conn.commit()
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in _TABLES:
            if not _has_persona_lock_fk(conn, table):
                _rebuild(conn, table)
        conn.commit()
    finally:
        if had_fk:
            conn.execute("PRAGMA foreign_keys=ON")
