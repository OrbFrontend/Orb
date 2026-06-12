"""0030_drop_vestigial_voice_profiles_and_feedback_columns — reconcile two pieces of
schema drift the fresh-install DDL (backend/database/schema.py) never carried, so a
migrated DB stops diverging from ``CREATE_TABLES_SQL``.

Both are the same class of bug 0028/0029 cleaned up: a migration left an artefact
behind that no later migration dropped, tripping the fresh-vs-migrated
schema-equivalence gate (backend/presets.py ``assert_schema_safe``) and so refusing
every preset export/snapshot/restore.

1. ``voice_profiles`` table. Added by 0015 (legacy TTS storage), then ported into
   ``character_cards.workflow_state["tts"]`` + ``settings.workflow_config["tts"]`` and
   dropped by 0020 (``_port_tts``). Fresh installs never create it. But some databases
   reached 0020 with the table already empty / its rows already ported and the legacy
   ``settings.tts_*`` columns already gone, and came out the far side still carrying an
   orphaned, never-read ``voice_profiles`` table. It is dropped here only when empty:
   on any DB that reaches 0030, 0020 has already run, so any real rows were ported
   long ago; a non-empty table would mean un-ported data, so we leave it for a human
   rather than silently lose it (the equivalence gate keeps complaining, which is the
   intended loud signal).

2. ``conversation_logs.reasoning_feedback`` and ``conversation_logs.feedback_latency_ms``.
   An early cut of the feedback sub-step (final form: migration 0024) gave feedback its
   own ``reasoning_feedback`` / ``feedback_latency_ms`` columns; that was consolidated
   into the single ``feedback`` JSON column 0024 actually ships, and 0024 explicitly
   does *not* add the two split columns. Databases that ran the early 0024 keep them as
   dead, never-read columns. They are plain TEXT/INTEGER with no FK, so a straight
   ``ALTER TABLE … DROP COLUMN`` suffices.

Idempotent: the table drop is skipped when the table is already absent (fresh installs,
or a DB already through 0030), and each column drop is skipped when already absent.
``ALTER TABLE … DROP COLUMN`` is the same mechanism 0016/0028/0029 use.
"""

from __future__ import annotations

import sqlite3

_STALE_LOG_COLUMNS = ("reasoning_feedback", "feedback_latency_ms")


def migrate(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "voice_profiles" in tables:
        rows = conn.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0]
        if rows == 0:
            conn.execute("DROP TABLE voice_profiles")
            print("[migrations] 0030: dropped vestigial empty voice_profiles table")
        else:
            # Un-ported rows: refuse to drop and lose data. The equivalence gate stays
            # red on purpose so this surfaces for a human instead of vanishing.
            print(
                f"[migrations] 0030: voice_profiles has {rows} row(s); leaving it in place "
                f"(0020 should have ported and dropped it — investigate before dropping)"
            )

    log_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_logs)").fetchall()}
    for col in _STALE_LOG_COLUMNS:
        if col in log_cols:
            conn.execute(f"ALTER TABLE conversation_logs DROP COLUMN {col}")
            print(f"[migrations] 0030: dropped vestigial conversation_logs.{col}")
