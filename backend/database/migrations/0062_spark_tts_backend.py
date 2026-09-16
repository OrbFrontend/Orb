"""Keep existing Spark-TTS sidecar profiles working after the built-in backend."""

from __future__ import annotations

import sqlite3


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def migrate(conn: sqlite3.Connection) -> None:
    # Idempotent: the second run matches no rows. Keep the update scoped to
    # the three places where a TTS profile is persisted.
    for table in ("character_cards", "group_members"):
        if not _has_table(conn, table):
            continue
        cursor = conn.execute(
            f"UPDATE {table} SET workflow_state = json_set(workflow_state, '$.tts.backend', 'spark_remote') "  # nosec B608 -- literal table names
            "WHERE json_valid(workflow_state) AND json_extract(workflow_state, '$.tts.backend') = 'spark'"
        )
        if cursor.rowcount:
            print(f"[migrations] 0062: moved {cursor.rowcount} {table} voice profile(s) to the spark_remote backend")

    if _has_table(conn, "workflow_attachments"):
        cursor = conn.execute(
            "UPDATE workflow_attachments "
            "SET generation_metadata = json_set(generation_metadata, '$.backend', 'spark_remote') "
            "WHERE workflow_id = 'tts' AND json_valid(generation_metadata) "
            "AND json_extract(generation_metadata, '$.backend') = 'spark'"
        )
        if cursor.rowcount:
            print(f"[migrations] 0062: moved {cursor.rowcount} stored speech record(s) to the spark_remote backend")
