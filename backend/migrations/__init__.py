"""
backend/migrations/__init__.py — lightweight migration runner.

To add a new migration:
  1. Create backend/migrations/NNNN_description.py with a migrate(conn) function.
  2. Append its module name (without .py) to MIGRATIONS below.

Applied migrations are recorded in the `schema_migrations` table so each runs
exactly once, even across restarts.
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

# Ordered list of migration module names. Append new entries at the bottom.
MIGRATIONS: list[str] = [
    "0001_editor_rename",
    "0002_cleanup_orphaned_messages",
    "0003_create_default_persona",
    "0004_director_fragments",
    "0005_keywords_director_fragment",
    "0006_rename_fragments_to_mood_fragments",
    "0007_add_user_personas_columns",
    "0008_multi_endpoints",
    "0009_shared_system_prompt",
    "0010_active_model_config_on_endpoints",
    "0011_add_show_editor_diff",
    "0012_hide_streaming_until_baked",
]


def run_pending(db_path: str | Path) -> None:
    """Apply all unapplied migrations against db_path."""
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

        for name in MIGRATIONS:
            if name in applied:
                continue
            mod = importlib.import_module(f"backend.migrations.{name}")
            mod.migrate(conn)
            conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (name,))
            conn.commit()
            print(f"[migrations] Applied: {name}")

    finally:
        conn.close()
