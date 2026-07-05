"""
0037_local_ml_enabled -- add the per-feature local-ML on/off map to settings.

`local_ml_enabled` (default '{}') holds a `{feature: bool}` map where a missing
key means enabled, mirroring `workflow_enabled`. Written only via a per-key
json_set (set_local_ml_enabled), never the whole column.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "local_ml_enabled" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN local_ml_enabled TEXT NOT NULL DEFAULT '{}'")
        print("[migrations] 0037: added local_ml_enabled column to settings")
