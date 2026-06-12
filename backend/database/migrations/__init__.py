"""
backend/database/migrations/__init__.py — lightweight migration runner.

To add a new migration, create backend/database/migrations/NNNN_description.py
with a migrate(conn) function.

Applied migrations are recorded in the `schema_migrations` table so each runs
exactly once, even across restarts.
"""

from __future__ import annotations

import importlib
import re
import sqlite3
from pathlib import Path

_MIGRATION_RE = re.compile(r"^\d{4}_")

MIGRATIONS: list[str] = sorted(p.stem for p in Path(__file__).parent.glob("*.py") if _MIGRATION_RE.match(p.name))


def run_pending(db_path: str | Path) -> int:
    """Apply all unapplied migrations against db_path.

    Returns the number of migrations applied (0 when already current), so a
    caller holding a private copy (restore_full) can skip its post-migration
    VACUUM when nothing changed.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id       TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()

        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}

        count = 0
        for name in MIGRATIONS:
            if name in applied:
                continue
            mod = importlib.import_module(f"backend.database.migrations.{name}")
            mod.migrate(conn)
            conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (name,))
            conn.commit()
            print(f"[migrations] Applied: {name}")
            count += 1
        return count

    finally:
        conn.close()
