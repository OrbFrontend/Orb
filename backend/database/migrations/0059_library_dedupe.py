"""Add duplicate dismissals stamped with both body hashes, plus cached avatar hashes.

Use canonical table DDL. Only avatar decoding is cached; its stamp combines
DEDUPE_REVISION and updated_at.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- literal table names


def migrate(conn: sqlite3.Connection) -> None:
    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='duplicate_dismissals'").fetchone()
    conn.execute(table_create_sql("duplicate_dismissals"))
    if not existed:
        print("[migrations] 0059: created duplicate_dismissals")

    card_cols = _columns(conn, "character_cards")
    if not card_cols:
        return
    for column in ("avatar_dhash", "avatar_dhash_stamp"):
        if column not in card_cols:
            conn.execute(f"ALTER TABLE character_cards ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")  # nosec B608
            print(f"[migrations] 0059: added {column} column to character_cards")
