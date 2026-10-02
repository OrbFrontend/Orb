"""Persist the epoch used to reject writes from before a restore."""

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS dataset_meta (id INTEGER PRIMARY KEY CHECK (id = 1), epoch TEXT NOT NULL)")
