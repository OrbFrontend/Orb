"""0038_character_expressions -- add the character_expressions table.

Per-character expression images (go-emotions label -> PNG), keyed by
(character_card_id, label), CASCADE-deleted with the card. Fresh installs get
this from schema.py; this backfills existing DBs. DDL sourced from schema.py so
the two shapes cannot diverge.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(table_create_sql("character_expressions"))
    print("[migrations] 0038: ensured character_expressions table")
