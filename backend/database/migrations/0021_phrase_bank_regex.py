"""
0021_phrase_bank_regex -- add `kind` and `pattern` columns to phrase_bank.

A phrase-bank group is now one of two kinds:
  * 'literal' (default) — equivalent variant phrases stored in `variants`.
  * 'regex'             — a single regular expression stored in `pattern`.

Existing rows are literal groups, so the column defaults preserve them.
"""

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
