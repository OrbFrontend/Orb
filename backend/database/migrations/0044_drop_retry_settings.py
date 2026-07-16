"""
0044_drop_retry_settings -- remove the transient-error retry settings columns.

Retry is now always on with fixed defaults (inference/retry.py): exposing error
handling as a user toggle was bad UX, and nobody can sensibly tune it. Drop the
columns 0040 added so stored DBs (and imported presets, which replay migrations)
carry no trace of the retired setting.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    for col in ("retry_enabled", "retry_count", "retry_delay_seconds"):
        if col in cols:
            conn.execute(f"ALTER TABLE settings DROP COLUMN {col}")  # nosec B608 — literal names
            print(f"[migrations] 0044: dropped {col} column from settings")
