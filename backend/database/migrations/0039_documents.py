"""0039_documents -- add the documents table.

Free-form Document mode: one row per document (plain ``content`` plus opaque
JS-domain ``generated_spans`` offsets). Fresh installs get this from schema.py;
this backfills existing DBs. DDL sourced from schema.py so the two shapes cannot
diverge.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(table_create_sql("documents"))
    print("[migrations] 0039: ensured documents table")
