"""0031_drop_vestigial_tts_scripter_columns — drop the dead
``settings.tts_scripter_enabled`` and ``settings.tts_scripter_prompt`` columns.

The detached LLM speech-scripter feature (added by 84bf39e "feat(tts): add voice
sidepanel controls") put these two columns on ``settings`` via the then-monolithic
``backend/database.py`` init path: fresh installs got them in the CREATE TABLE and
existing installs via inline ``ALTER TABLE … ADD COLUMN``. The feature was removed
shortly after (16a4288 "refactor(tts): remove detached LLM speech scripter"), which
deleted the DDL and the ALTERs — but no migration ever dropped the columns from
databases that booted in that window, so those installs carry two vestigial,
never-read columns.

Same class as 0028/0029/0030: the live schema diverges from ``CREATE_TABLES_SQL``
by exactly these columns, tripping the fresh-vs-migrated schema-equivalence gate
(backend/presets.py ``assert_schema_safe``) and refusing every preset
export/snapshot/restore. Found by simulating a fresh install at every historical
DDL version and migrating it to HEAD; this was the one residual divergence.

Both columns are confirmed unreferenced by application code (the scripter's runtime
read them from a settings dict that no longer contains them). Plain INTEGER/TEXT,
no FK clause, so a straight ``ALTER TABLE … DROP COLUMN`` suffices — no FK toggle
needed, unlike 0029.

Idempotent: each column is skipped when already absent (fresh installs, installs
that never booted in the scripter window, or a DB already through 0031).
"""

from __future__ import annotations

import sqlite3

_VESTIGIAL = ("tts_scripter_enabled", "tts_scripter_prompt")


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    for col in _VESTIGIAL:
        if col in cols:
            conn.execute(f"ALTER TABLE settings DROP COLUMN {col}")
            print(f"[migrations] 0031: dropped vestigial settings.{col}")
