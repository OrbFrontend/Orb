"""Remove schema artifacts left by superseded features."""

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
