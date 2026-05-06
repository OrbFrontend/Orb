"""
0015_voice_profiles -- add TTS voice profiles and playback settings.
"""

from __future__ import annotations

import json
import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "voice_profiles" not in tables:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                character_card_id TEXT NOT NULL UNIQUE,
                backend TEXT NOT NULL DEFAULT 'edge',
                voice_id TEXT NOT NULL DEFAULT 'en-US-JennyNeural',
                language TEXT NOT NULL DEFAULT 'en-US',
                rate REAL NOT NULL DEFAULT 1.0,
                pitch REAL NOT NULL DEFAULT 1.0,
                enabled INTEGER NOT NULL DEFAULT 0,
                endpoint_id INTEGER,
                scripter_model TEXT DEFAULT '',
                scripter_temperature REAL DEFAULT 0.3,
                speech_prompt TEXT DEFAULT '',
                api_url TEXT DEFAULT '',
                api_key TEXT DEFAULT '',
                model TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (character_card_id) REFERENCES character_cards(id)
            )
            """
        )
        print("[migrations] 0015: created voice_profiles table")

    if "settings" in tables:
        settings_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()
        }
        if "tts_auto_speak" not in settings_cols:
            conn.execute(
                "ALTER TABLE settings ADD COLUMN tts_auto_speak INTEGER NOT NULL DEFAULT 0"
            )
        if "tts_volume" not in settings_cols:
            conn.execute(
                "ALTER TABLE settings ADD COLUMN tts_volume REAL NOT NULL DEFAULT 0.75"
            )

        row = conn.execute(
            "SELECT id, reasoning_enabled_passes FROM settings WHERE id = 1"
        ).fetchone()
        if row:
            try:
                passes = json.loads(row[1] or "{}")
            except json.JSONDecodeError:
                passes = {}
            if "scripter" in passes:
                passes.pop("scripter", None)
                conn.execute(
                    "UPDATE settings SET reasoning_enabled_passes = ? WHERE id = 1",
                    (json.dumps(passes),),
                )
