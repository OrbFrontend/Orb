"""0028_drop_vestigial_schema_artifacts — drop every table/column an earlier build
left behind that the fresh-install DDL (backend/database/schema.py) never carried,
so a migrated DB stops diverging from ``CREATE_TABLES_SQL``.

All four artefact groups are the same class of bug: a feature (or an early cut of
one) shipped schema via a since-rewritten migration or the old inline ``init_db``
path, the feature was removed or redesigned, and nothing dropped the leftovers from
databases that booted in the window. Each tripped the fresh-vs-migrated
schema-equivalence gate (backend/features/presets/engine.py ``assert_schema_safe``), refusing every
preset export/snapshot/restore. The full inventory, found by fresh-installing every
historical DDL version in git history and migrating it to HEAD:

1. ``settings.active_model_config_id`` — superseded when the active-model pointer
   moved to ``endpoints.active_model_config_id`` (migration 0010); the old
   settings-level pointer was never read again and never dropped.
2. ``settings.active_agent_endpoint_id`` / ``settings.active_agent_model_config_id``
   — an early version of the agent-endpoint feature (later rewritten into what is
   now 0013) put this pointer pair on ``settings``; the redesign kept only
   ``settings.agent_endpoint_id`` + ``endpoints.agent_active_model_config_id``.
3. ``voice_profiles`` table and ``conversation_logs.reasoning_feedback`` /
   ``conversation_logs.feedback_latency_ms`` — legacy TTS storage (0015) ported and
   dropped by 0020, but re-created by bootstrap while the table was still in the
   then-current DDL; and an early cut of the feedback sub-step whose split columns
   0024 consolidated into the single ``feedback`` JSON column.
4. ``settings.tts_scripter_enabled`` / ``settings.tts_scripter_prompt`` — the
   detached LLM speech scripter (84bf39e), removed by 16a4288, which deleted the
   DDL and inline ALTERs but not the columns already on disk.

``voice_profiles`` is dropped only when empty: on any DB that reaches 0028, 0020
has already run, so any real rows were ported long ago; a non-empty table would
mean un-ported data, so we leave it for a human rather than silently lose it (the
equivalence gate keeps complaining, which is the intended loud signal).

Idempotent: every drop is skipped when the table/column is already absent (fresh
installs, or a DB already through 0028). ``ALTER TABLE … DROP COLUMN`` is the same
mechanism migration 0016 uses; foreign keys are flipped off for the ``settings``
column drops since several carry a ``REFERENCES`` clause.
"""

from __future__ import annotations

import sqlite3

_VESTIGIAL_SETTINGS_COLUMNS = (
    "active_model_config_id",
    "active_agent_endpoint_id",
    "active_agent_model_config_id",
    "tts_scripter_enabled",
    "tts_scripter_prompt",
)
_VESTIGIAL_LOG_COLUMNS = ("reasoning_feedback", "feedback_latency_ms")


def migrate(conn: sqlite3.Connection) -> None:
    settings_cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    to_drop = [c for c in _VESTIGIAL_SETTINGS_COLUMNS if c in settings_cols]
    if to_drop:
        # PRAGMA foreign_keys is a no-op inside a transaction; the runner has
        # committed before this call. Flip FKs off for the column drops (several
        # carry a REFERENCES clause), then restore prior state.
        conn.commit()
        had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            for col in to_drop:
                conn.execute(f"ALTER TABLE settings DROP COLUMN {col}")
                conn.commit()
                print(f"[migrations] 0028: dropped vestigial settings.{col}")
        finally:
            if had_fk:
                conn.execute("PRAGMA foreign_keys=ON")

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "voice_profiles" in tables:
        rows = conn.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0]
        if rows == 0:
            conn.execute("DROP TABLE voice_profiles")
            print("[migrations] 0028: dropped vestigial empty voice_profiles table")
        else:
            # Un-ported rows: refuse to drop and lose data. The equivalence gate stays
            # red on purpose so this surfaces for a human instead of vanishing.
            print(
                f"[migrations] 0028: voice_profiles has {rows} row(s); leaving it in place "
                f"(0020 should have ported and dropped it — investigate before dropping)"
            )

    log_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_logs)").fetchall()}
    for col in _VESTIGIAL_LOG_COLUMNS:
        if col in log_cols:
            conn.execute(f"ALTER TABLE conversation_logs DROP COLUMN {col}")
            print(f"[migrations] 0028: dropped vestigial conversation_logs.{col}")
