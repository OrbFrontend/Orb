"""Add per-model reasoning effort and custom body key/value columns.

Empty effort uses provider defaults. Custom values are JSON-decoded when
possible, otherwise sent as strings; validation stays in the API.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(model_configs)").fetchall()}
    for col in ("reasoning_effort", "reasoning_effort_param", "reasoning_effort_value"):
        if col not in cols:
            conn.execute(f"ALTER TABLE model_configs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            print(f"[migrations] 0043: added {col} column to model_configs")
