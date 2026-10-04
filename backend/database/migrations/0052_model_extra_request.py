"""Add per-model extra_headers and extra_body, defaulting to empty.

Headers apply to both transports; extra body fields apply to chat only. Validation stays in the API.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(model_configs)").fetchall()}
    for col in ("extra_headers", "extra_body"):
        if col not in cols:
            conn.execute(f"ALTER TABLE model_configs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            print(f"[migrations] 0052: added {col} column to model_configs")
