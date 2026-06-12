"""0028_drop_vestigial_settings_model_config — drop the dead
``settings.active_model_config_id`` column.

Migration 0008 added ``active_model_config_id`` to ``settings`` for the original
single-active-model design. 0010 moved the active-model concept onto
``endpoints`` (``endpoints.active_model_config_id``), and the column was removed
from the fresh-install DDL (backend/database/schema.py) — but no migration ever
dropped it from databases that ran 0008, so every migrated install (including a
fresh one, which still runs 0008's ALTER) carries a vestigial, never-read
``settings.active_model_config_id``.

That left the live schema diverging from ``CREATE_TABLES_SQL`` by exactly this
column. Dropping it reconciles the two so the fresh-vs-migrated schema-equivalence
gate (backend/presets.py ``assert_schema_safe``) holds. The column is confirmed
unreferenced by application code; only ``endpoints.active_model_config_id`` is used.

Idempotent: skipped when the column is already absent (a DB never through 0008, or
already through 0028). ``ALTER TABLE … DROP COLUMN`` is the same mechanism migration
0016 uses; foreign keys are flipped off for the change since the column carries a
``REFERENCES model_configs(id)`` clause.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "active_model_config_id" not in cols:
        return
    # PRAGMA foreign_keys is a no-op inside a transaction; the runner has committed
    # before this call. Flip FKs off for the column drop, then restore prior state.
    conn.commit()
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("ALTER TABLE settings DROP COLUMN active_model_config_id")
        conn.commit()
        print("[migrations] 0028: dropped vestigial settings.active_model_config_id")
    finally:
        if had_fk:
            conn.execute("PRAGMA foreign_keys=ON")
