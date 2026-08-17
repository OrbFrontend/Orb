"""0054_group_chats -- durable group rosters and per-message speaker identity."""

from __future__ import annotations

import sqlite3

from backend.database import schema


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608


def migrate(conn: sqlite3.Connection) -> None:
    conv = _columns(conn, "conversations")
    additions = (
        ("kind", "TEXT NOT NULL DEFAULT 'solo' CHECK (kind IN ('solo', 'group'))"),
        (
            "group_turn_mode",
            "TEXT NOT NULL DEFAULT 'director' CHECK (group_turn_mode IN ('manual', 'round_robin', 'director'))",
        ),
        ("group_max_speakers", "INTEGER NOT NULL DEFAULT 3 CHECK (group_max_speakers BETWEEN 1 AND 8)"),
        (
            "group_context_mode",
            "TEXT NOT NULL DEFAULT 'private' CHECK (group_context_mode IN ('private', 'shared', 'swap'))",
        ),
    )
    for name, ddl in additions:
        if name not in conv:
            conn.execute(f"ALTER TABLE conversations ADD COLUMN {name} {ddl}")  # nosec B608

    conn.execute(schema.table_create_sql("group_members"))

    messages = _columns(conn, "messages")
    if "speaker_member_id" not in messages:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN speaker_member_id TEXT DEFAULT NULL "
            "REFERENCES group_members(id) ON DELETE SET NULL"
        )
    if "beat_id" not in messages:
        conn.execute("ALTER TABLE messages ADD COLUMN beat_id TEXT DEFAULT NULL")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_group_member_active_card "
        "ON group_members(conversation_id, character_card_id) "
        "WHERE active = 1 AND character_card_id IS NOT NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_beat ON messages(conversation_id, beat_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_speaker ON messages(speaker_member_id)")
