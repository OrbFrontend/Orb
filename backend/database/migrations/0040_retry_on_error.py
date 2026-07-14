"""
0040_retry_on_error -- add the transient-error retry settings.

`retry_enabled` (default 0/off) toggles re-issuing a completion that failed with
a temporary server-side error; `retry_count` (default 10) is the number of
retries after the initial attempt; `retry_delay_seconds` (default 5) is the wait
between attempts. Fresh installs get these from schema.py; this backfills
existing DBs. The retryable status-code set is a code constant (inference/retry.py),
not a column.
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
