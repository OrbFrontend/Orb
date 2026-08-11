"""Upgrade paths for Dynamic Worlds (migrations 0053 and 0054).

Fresh installs are stamped past the migration chain, so
``test_fresh_install_stamping`` already proves ``schema.py`` and the migration
agree. What it cannot prove is what happens to a database that *already has
rows*: this drives 0053 against a pre-feature schema carrying real worlds and
lorebook entries and asserts the two things an upgrade must get right --
every existing entry backfills as ``authored`` (so the projection sees exactly
what it saw before), and the FK shape the preset engine derives its mechanics
from matches a fresh install's.
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from fastapi import FastAPI

import backend.api as api_module
import backend.database.connection as db_connection
from backend.database.migrations import MIGRATIONS
from backend.database.schema import CREATE_TABLES_SQL
from backend.features.presets import engine as presets

_MIGRATION = importlib.import_module("backend.database.migrations.0053_dynamic_worlds")
_STATUS_MIGRATION = importlib.import_module("backend.database.migrations.0054_world_changeset_superseded")

# The worlds/lorebook_entries shape immediately before 0053, plus the two tables
# world_changesets points at. Written out rather than derived, so the test still
# describes the "before" state once schema.py has moved on.
_PRE_0053_SQL = """
CREATE TABLE worlds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New Conversation',
    created_at TEXT NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE lorebook_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    case_insensitive BOOLEAN NOT NULL DEFAULT 1,
    constant BOOLEAN NOT NULL DEFAULT 0,
    at_depth INTEGER NOT NULL DEFAULT 0,
    use_regex INTEGER NOT NULL DEFAULT 0,
    selective INTEGER NOT NULL DEFAULT 0,
    secondary_keys TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 100,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_OLD_CHANGESETS_SQL = """
CREATE TABLE world_changesets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'rejected', 'stale', 'reverted')),
    base_revision INTEGER NOT NULL DEFAULT 0,
    applied_revision INTEGER DEFAULT NULL,
    source_user_message_id INTEGER DEFAULT NULL REFERENCES messages(id) ON DELETE SET NULL,
    source_assistant_message_id INTEGER DEFAULT NULL REFERENCES messages(id) ON DELETE SET NULL,
    source_conversation_id TEXT DEFAULT NULL REFERENCES conversations(id) ON DELETE SET NULL,
    source_character_label TEXT NOT NULL DEFAULT '',
    source_conversation_label TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'agent'
        CHECK (origin IN ('agent', 'undo', 'reset', 're_evaluate')),
    summary TEXT NOT NULL DEFAULT '',
    operations TEXT NOT NULL DEFAULT '[]',
    before_entries TEXT NOT NULL DEFAULT '[]',
    after_entries TEXT NOT NULL DEFAULT '[]',
    reverts_changeset_id INTEGER DEFAULT NULL REFERENCES world_changesets(id) ON DELETE SET NULL,
    supersedes_changeset_id INTEGER DEFAULT NULL REFERENCES world_changesets(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT DEFAULT NULL,
    applied_at TEXT DEFAULT NULL
);
CREATE INDEX idx_changeset_world_status ON world_changesets(world_id, status);
CREATE INDEX idx_changeset_source_asst ON world_changesets(source_assistant_message_id);
"""


def _upgraded(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.executescript(_PRE_0053_SQL)
    ts = "2024-01-01"
    conn.execute(
        "INSERT INTO worlds (id, name, created_at, updated_at) VALUES ('w1', 'Old World', ?, ?)",
        (ts, ts),
    )
    for name in ("Alpha", "Beta"):
        conn.execute(
            "INSERT INTO lorebook_entries (world_id, name, content, created_at, updated_at) VALUES ('w1', ?, 'body', ?, ?)",
            (name, ts, ts),
        )
    conn.commit()
    _MIGRATION.migrate(conn)
    conn.commit()
    return conn


def test_existing_entries_backfill_as_authored(tmp_path):
    conn = _upgraded(tmp_path)
    try:
        rows = conn.execute(
            "SELECT name, entry_layer, overlay_action, supersedes_entry_id, archived FROM lorebook_entries"
        ).fetchall()
        assert sorted(rows) == [
            ("Alpha", "authored", "", None, 0),
            ("Beta", "authored", "", None, 0),
        ]
    finally:
        conn.close()


def test_worlds_gain_their_opt_in_and_revision_stamp(tmp_path):
    conn = _upgraded(tmp_path)
    try:
        assert conn.execute("SELECT dynamic_enabled, content_revision FROM worlds").fetchone() == (0, 0)
    finally:
        conn.close()


def test_the_changeset_table_and_indexes_arrive(tmp_path):
    conn = _upgraded(tmp_path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
        assert "world_changesets" in names
        assert {
            "idx_lorebook_overlay",
            "idx_changeset_world_status",
            "idx_changeset_source_asst",
        } <= names
    finally:
        conn.close()


def test_a_database_that_already_ran_0053_gains_the_superseded_status(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "old-0053.db"))
    try:
        conn.executescript(_PRE_0053_SQL)
        conn.executescript(_OLD_CHANGESETS_SQL)
        conn.execute("INSERT INTO worlds (id, name, created_at, updated_at) VALUES ('w1', 'Old World', 't', 't')")
        conn.execute(
            "INSERT INTO world_changesets (world_id, status, summary, created_at) VALUES ('w1', 'stale', 'keep me', 't')"
        )
        conn.commit()

        _STATUS_MIGRATION.migrate(conn)
        conn.execute("UPDATE world_changesets SET status = 'superseded' WHERE id = 1")
        conn.commit()

        assert conn.execute("SELECT status, summary FROM world_changesets WHERE id = 1").fetchone() == ("superseded", "keep me")
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(world_changesets)")}
        assert {
            "idx_changeset_world_status",
            "idx_changeset_source_asst",
        } <= indexes
    finally:
        conn.close()


def test_the_migration_is_idempotent(tmp_path):
    """Re-running must not duplicate columns or wipe the backfill."""
    conn = _upgraded(tmp_path)
    try:
        _MIGRATION.migrate(conn)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM lorebook_entries WHERE entry_layer = 'authored'").fetchone()[0] == 2
    finally:
        conn.close()


async def test_existing_database_migrates_before_latest_schema_indexes_run(tmp_path, monkeypatch):
    """A real startup must add ``entry_layer`` before init_db creates its index.

    This is the upgrade shape that previously crashed with
    ``OperationalError: no such column: entry_layer`` before 0053 could run.
    """
    path = tmp_path / "pre-0053.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_PRE_0053_SQL)
        conn.execute("CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.executemany(
            "INSERT INTO schema_migrations (id) VALUES (?)",
            [(name,) for name in MIGRATIONS if name != "0053_dynamic_worlds"],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    monkeypatch.setattr(api_module, "DB_PATH", str(path))

    async with api_module.lifespan(FastAPI()):
        pass

    conn = sqlite3.connect(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(lorebook_entries)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(lorebook_entries)")}
        assert "entry_layer" in columns
        assert "idx_lorebook_overlay" in indexes
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE id = '0053_dynamic_worlds'").fetchone() == (1,)
    finally:
        conn.close()


def test_upgraded_fk_shape_matches_a_fresh_install(tmp_path):
    """The preset engine derives merge order and FK rewriting from the live
    schema, so an ALTER-added column whose FK differs from the canonical one is
    the exact class of bug that silently corrupts backups (see migration 0026)."""
    conn = _upgraded(tmp_path)
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(CREATE_TABLES_SQL)
        live = presets._build_schema_model(conn)
        canon = presets._build_schema_model(ref)
        for table in ("worlds", "lorebook_entries", "world_changesets"):
            assert set(live.tables[table].cols) == set(canon.tables[table].cols), table
            assert presets._edge_set(live.tables[table]) == presets._edge_set(canon.tables[table]), table
        assert ("lorebook_entries", "supersedes_entry_id") in live.deferred
    finally:
        ref.close()
        conn.close()
