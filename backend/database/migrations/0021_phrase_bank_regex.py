"""Add phrase-bank kind and pattern columns; existing groups default to literal."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(phrase_bank)").fetchall()}
    if "kind" not in cols:
        conn.execute("ALTER TABLE phrase_bank ADD COLUMN kind TEXT NOT NULL DEFAULT 'literal'")
        print("[migrations] 0021: added kind column to phrase_bank")
    if "pattern" not in cols:
        conn.execute("ALTER TABLE phrase_bank ADD COLUMN pattern TEXT")
        print("[migrations] 0021: added pattern column to phrase_bank")
