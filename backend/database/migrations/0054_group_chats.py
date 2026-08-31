"""Add group conversations, rosters, and sheet proposals."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from backend.database import schema

_CONVERSATION_COLUMNS = (
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
    ("group_sheet_updates", "INTEGER NOT NULL DEFAULT 0 CHECK (group_sheet_updates IN (0, 1))"),
    ("group_root_id", "TEXT DEFAULT NULL REFERENCES conversations(id) ON DELETE SET NULL"),
)

# Added after group_members exists: speaker_member_id points at it.
_MESSAGE_COLUMNS = (
    ("speaker_member_id", "TEXT DEFAULT NULL REFERENCES group_members(id) ON DELETE SET NULL"),
    ("exchange_id", "TEXT DEFAULT NULL"),
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_conversations_group_root ON conversations(group_root_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_group_member_active_card "
    "ON group_members(conversation_id, character_card_id) "
    "WHERE active = 1 AND character_card_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_messages_exchange ON messages(conversation_id, exchange_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_speaker ON messages(speaker_member_id)",
    "CREATE INDEX IF NOT EXISTS idx_sheet_proposal_conv_status ON member_sheet_proposals(conversation_id, status)",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 — literal table name


def _add_columns(conn: sqlite3.Connection, table: str, additions: Sequence[tuple[str, str]]) -> None:
    existing = _columns(conn, table)
    for name, ddl in additions:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")  # nosec B608 — names from a module constant
            print(f"[migrations] 0054: added {table}.{name}")


def migrate(conn: sqlite3.Connection) -> None:
    _add_columns(conn, "conversations", _CONVERSATION_COLUMNS)

    # Both tables come from the canonical fresh-install DDL rather than a pasted
    # copy: group_members carries a UNIQUE constraint and two CHECKs,
    # member_sheet_proposals a status CHECK and two cascading edges.
    conn.execute(schema.table_create_sql("group_members"))
    conn.execute(schema.table_create_sql("member_sheet_proposals"))

    _add_columns(conn, "messages", _MESSAGE_COLUMNS)

    for sql in _INDEXES:
        conn.execute(sql)
