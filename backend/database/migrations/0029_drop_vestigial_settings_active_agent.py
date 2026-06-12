"""0029_drop_vestigial_settings_active_agent — drop the dead
``settings.active_agent_endpoint_id`` and ``settings.active_agent_model_config_id``
columns.

An early version of the agent-endpoint feature (later rewritten into what is now
migration 0013) added an *active agent* pointer pair directly to ``settings``:
``active_agent_endpoint_id`` (REFERENCES endpoints) and
``active_agent_model_config_id`` (REFERENCES model_configs). The feature was then
redesigned: the writer/agent split moved onto ``endpoints``
(``endpoints.agent_active_model_config_id``) and ``settings`` kept only
``agent_endpoint_id`` / ``agent_same_as_writer``. The current 0013 adds that final
shape, and the fresh-install DDL (backend/database/schema.py) never carried the
``active_agent_*`` pair — but no migration ever dropped them from databases that
ran the old 0013, so those installs carry two vestigial, never-read columns.

That left the live schema diverging from ``CREATE_TABLES_SQL`` by exactly these two
columns (and their FKs), tripping the fresh-vs-migrated schema-equivalence gate
(backend/presets.py ``assert_schema_safe``). Dropping them reconciles the two. Both
columns are confirmed unreferenced by application code; only the surviving
``settings.agent_endpoint_id`` and ``endpoints.agent_active_model_config_id`` are
used.

Idempotent: each column is skipped when already absent (a DB that ran only the
current 0013, or one already through 0029). ``ALTER TABLE … DROP COLUMN`` is the
same mechanism migrations 0016 and 0028 use; foreign keys are flipped off for the
change since each column carries a ``REFERENCES`` clause.
"""

from __future__ import annotations

import sqlite3

_VESTIGIAL = ("active_agent_endpoint_id", "active_agent_model_config_id")


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    to_drop = [c for c in _VESTIGIAL if c in cols]
    if not to_drop:
        return
    # PRAGMA foreign_keys is a no-op inside a transaction; the runner has committed
    # before this call. Flip FKs off for the column drops, then restore prior state.
    conn.commit()
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for col in to_drop:
            conn.execute(f"ALTER TABLE settings DROP COLUMN {col}")
            conn.commit()
            print(f"[migrations] 0029: dropped vestigial settings.{col}")
    finally:
        if had_fk:
            conn.execute("PRAGMA foreign_keys=ON")
