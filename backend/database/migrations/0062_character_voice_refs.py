"""
0062_character_voice_refs -- the built-in Spark-TTS voice cloner.

Two changes, both about not breaking a working setup:

1. `character_voice_refs` holds the six-second reference clip a cloned voice
   was enrolled from. Its own table rather than a column on `character_cards`,
   because the card row is read on every turn and the clip is ~192 KB.

2. The `spark` TTS backend used to mean "the OrbTTS sidecar at
   http://localhost:9300"; it now means the built-in cloner, which needs a
   downloaded model and an enrolled voice and has no api_url at all. A profile
   written before this release names a server the user installed and may still
   be running, so those are moved to `spark_remote` -- the same adapter under
   its new name -- rather than being silently repointed at a model that is
   probably not on disk. New profiles get the built-in.

The rewrite covers all three places a voice profile is persisted: a character's
own state, a group member's override, and the reproduction record on an
already-generated audio attachment. Missing the third would leave a reroll of
an old clip trying to speak through a backend that has no voice enrolled.
"""

from __future__ import annotations

import sqlite3


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def migrate(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "character_voice_refs"):
        conn.execute(
            """
            CREATE TABLE character_voice_refs (
                character_card_id TEXT PRIMARY KEY REFERENCES character_cards(id) ON DELETE CASCADE,
                data_b64 TEXT NOT NULL,
                mime TEXT NOT NULL DEFAULT 'audio/wav',
                source_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        print("[migrations] 0062: created character_voice_refs")

    # json_set on a row whose json_extract already reads 'spark' -- so this is
    # idempotent (a second run matches nothing) and leaves every other backend,
    # and every row with no tts profile at all, untouched.
    for table, column in (
        ("character_cards", "workflow_state"),
        ("group_members", "workflow_state"),
    ):
        if not _has_table(conn, table):
            continue
        cursor = conn.execute(
            f"UPDATE {table} SET {column} = json_set({column}, '$.tts.backend', 'spark_remote') "  # nosec B608 -- literal names
            f"WHERE json_valid({column}) AND json_extract({column}, '$.tts.backend') = 'spark'"
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
