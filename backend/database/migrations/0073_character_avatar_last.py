"""Move avatar_b64 to character_cards' last column.

This avoids walking large overflow blobs when reading later metadata columns.
Queries use named columns; startup VACUUM reclaims old pages. Reruns are a no-op.
"""

from __future__ import annotations

import sqlite3

from backend.database import schema

_TABLE = "character_cards"


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate(conn: sqlite3.Connection) -> None:
    old_cols = _columns(conn, _TABLE)
    if not old_cols or old_cols[-1] == "avatar_b64":
        return
    # PRAGMA foreign_keys is a no-op inside a transaction, and dropping a parent
    # table under enforcement would cascade into character_expressions. Close
    # any stray transaction, turn FKs off for the rebuild, then restore them.
    conn.commit()
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        # One explicit transaction: sqlite3 opens none for CREATE TABLE, so a
        # failed copy would otherwise leave character_cards_new behind and every
        # later attempt would stop at "table already exists".
        conn.execute("BEGIN")
        try:
            block = schema.table_create_sql(_TABLE)
            conn.execute(block.replace(f"CREATE TABLE IF NOT EXISTS {_TABLE}", f"CREATE TABLE {_TABLE}_new", 1))
            present = set(old_cols)
            cols = ", ".join(c for c in _columns(conn, f"{_TABLE}_new") if c in present)
            conn.execute(f"INSERT INTO {_TABLE}_new ({cols}) SELECT {cols} FROM {_TABLE}")  # nosec B608 — schema-derived identifiers
            conn.execute(f"DROP TABLE {_TABLE}")
            conn.execute(f"ALTER TABLE {_TABLE}_new RENAME TO {_TABLE}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        if had_fk:
            conn.execute("PRAGMA foreign_keys=ON")
    print(f"[migrations] 0073: rebuilt {_TABLE} with avatar_b64 last")
