"""Add per-feature Local ML configuration as JSON, defaulting to {}.

Each feature owns its shape; the rewriter requires a selected model.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if not cols:
        # No settings table at all. PRAGMA table_info is silent about that and
        # the ALTER would abort the whole chain, taking startup with it -- but
        # init_db runs straight after and creates the table from schema.py, with
        # this column already on it. Skipping is self-healing; crashing is not.
        return
    if "local_ml_config" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN local_ml_config TEXT NOT NULL DEFAULT '{}'")
        print("[migrations] 0055: added local_ml_config column to settings")
