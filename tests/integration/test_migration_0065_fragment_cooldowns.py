"""Upgrade coverage for fragment cooldown columns."""

from __future__ import annotations

import importlib
import sqlite3

_MIGRATION = importlib.import_module("backend.database.migrations.0065_fragment_cooldowns")


def _pre_0065() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE mood_fragments (id TEXT PRIMARY KEY);
        CREATE TABLE interactive_fragments (id TEXT PRIMARY KEY);
        CREATE TABLE messages (id INTEGER PRIMARY KEY);
        INSERT INTO mood_fragments VALUES ('tense');
        INSERT INTO interactive_fragments VALUES ('pacing');
        INSERT INTO messages VALUES (1);
        """
    )
    return conn


def test_adds_fragment_configuration_and_message_snapshot_columns():
    conn = _pre_0065()
    _MIGRATION.migrate(conn)

    assert conn.execute("SELECT cooldown_turns FROM mood_fragments").fetchone() == (0,)
    assert conn.execute("SELECT cooldown_turns FROM interactive_fragments").fetchone() == (0,)
    assert conn.execute("SELECT fragment_cooldowns FROM messages").fetchone() == ("{}",)


def test_migration_is_idempotent():
    conn = _pre_0065()
    _MIGRATION.migrate(conn)
    _MIGRATION.migrate(conn)

    assert [row[1] for row in conn.execute("PRAGMA table_info(messages)")].count("fragment_cooldowns") == 1
