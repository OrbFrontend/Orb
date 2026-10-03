"""Add transient retry settings: disabled, 10 retries, 5s delay by default.
Retryable status codes remain in inference/retry.py.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "retry_enabled" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN retry_enabled INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0040: added retry_enabled column to settings")
    if "retry_count" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 10")
        print("[migrations] 0040: added retry_count column to settings")
    if "retry_delay_seconds" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN retry_delay_seconds REAL NOT NULL DEFAULT 5")
        print("[migrations] 0040: added retry_delay_seconds column to settings")
