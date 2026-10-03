"""Drop retry settings added by 0040; inference/retry.py now supplies fixed defaults."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    for col in ("retry_enabled", "retry_count", "retry_delay_seconds"):
        if col in cols:
            conn.execute(f"ALTER TABLE settings DROP COLUMN {col}")  # nosec B608 — literal names
            print(f"[migrations] 0044: dropped {col} column from settings")
