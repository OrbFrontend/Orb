"""Add miner suggestions, dismissals and staleness tables using canonical schema DDL."""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def migrate(conn: sqlite3.Connection) -> None:
    for table in ("slop_suggestions", "slop_dismissals", "slop_mining_state"):
        conn.execute(table_create_sql(table))
