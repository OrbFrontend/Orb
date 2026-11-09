"""Store the Inspector's per-step generation timings."""

import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA table_info(conversation_logs)").fetchall():
        add_columns(conn, "conversation_logs", "latency_breakdown TEXT", migration="0087")
