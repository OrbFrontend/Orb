"""
0051_community_extensions -- persistence for the community extension tier.

Adds the three package tables (``extension_packages``,
``extension_revisions``, ``extension_secrets``) and
``interactive_fragments.type_config``, the per-instance configuration blob an
extension-defined fragment type reads.

The tables are created from ``schema.table_create_sql`` rather than a pasted
copy, so an upgraded database gets byte-identical DDL to a fresh install --
the preset engine's schema-equivalence gate compares the two, and a hand-copied
``CREATE TABLE`` here is exactly how they drift.

``type_config`` lands on every install, not just ones that will use it: a
column that appears only once an extension is installed would make the fragment
CRUD path branch on whether it exists.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql

_TABLES = ("extension_packages", "extension_revisions", "extension_secrets")


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    if "type_config" not in cols:
        conn.execute("ALTER TABLE interactive_fragments ADD COLUMN type_config TEXT NOT NULL DEFAULT '{}'")
        print("[migrations] 0051: added type_config column to interactive_fragments")

    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    for table in _TABLES:
        if table in existing:
            continue
        conn.execute(table_create_sql(table))
        print(f"[migrations] 0051: created {table}")
