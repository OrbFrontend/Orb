"""Add library_tags vocabulary and card auto-tag staleness stamps.

Use canonical table DDL. Runs rewrite the card's tags when vocabulary
or card content changes.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- literal table names


def migrate(conn: sqlite3.Connection) -> None:
    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_tags'").fetchone()
    conn.execute(table_create_sql("library_tags"))
    if not existed:
        print("[migrations] 0058: created library_tags")

    card_cols = _columns(conn, "character_cards")
    if not card_cols:
        return
    for column in ("auto_tag_vocab_hash", "auto_tag_card_updated_at"):
        if column not in card_cols:
            conn.execute(f"ALTER TABLE character_cards ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")  # nosec B608
            print(f"[migrations] 0058: added {column} column to character_cards")
