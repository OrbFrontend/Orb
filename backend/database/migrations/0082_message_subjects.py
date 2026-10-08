"""Add the message_subjects table."""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(table_create_sql("message_subjects"))
