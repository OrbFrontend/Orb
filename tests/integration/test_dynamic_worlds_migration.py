"""The Dynamic Worlds upgrade path (migration 0053).

Fresh installs are stamped past the migration chain, so
``test_fresh_install_stamping`` already proves ``schema.py`` and the migrations
agree. What it cannot prove is what happens to a database that *already has
rows*: this drives 0053 against a pre-feature schema carrying real worlds and
lorebook entries and asserts what an upgrade must get right -- every existing
entry backfills as ``authored`` with its id intact (so the projection sees
exactly what it saw before), the overlay pointer survives a delete of what it
targets, and the shape the preset engine derives its mechanics from matches a
fresh install's, columns and FK edges alike.
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


def _seeded(path: Path) -> sqlite3.Connection:
    """A pre-feature database holding one World and two authored entries."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_PRE_0053_SQL)
    ts = "2024-01-01"
    conn.execute("INSERT INTO worlds (id, name, created_at, updated_at) VALUES ('w1', 'Old World', ?, ?)", (ts, ts))
    for name in ("Alpha", "Beta"):
        conn.execute(
            "INSERT INTO lorebook_entries (world_id, name, content, created_at, updated_at) VALUES ('w1', ?, 'body', ?, ?)",
            (name, ts, ts),
        )
    conn.commit()
    return conn


def _upgraded(tmp_path: Path) -> sqlite3.Connection:
    conn = _seeded(tmp_path / "old.db")
    _MIGRATION.migrate(conn)
    conn.commit()
    return conn


def test_existing_entries_backfill_as_authored(tmp_path):
    """The rebuild is the backfill: ids and columns carry over, new columns default."""
    conn = _upgraded(tmp_path)
    try:
        rows = conn.execute(
            "SELECT id, name, content, entry_layer, overlay_action, supersedes_entry_id, archived"
            " FROM lorebook_entries ORDER BY id"
        ).fetchall()
        assert rows == [
            (1, "Alpha", "body", "authored", "", None, 0),
            (2, "Beta", "body", "authored", "", None, 0),
        ]
    finally:
        conn.close()


def test_deleting_the_authored_target_keeps_the_overlay(tmp_path):
    """``supersedes_entry_id`` points from an Agent overlay row at the authored
    row it hides, so deleting that authored entry -- the natural cleanup after
    accepting a ``replace`` -- must drop the pointer, not the reviewed lore."""
    conn = _upgraded(tmp_path)
    try:
        conn.execute(
            "INSERT INTO lorebook_entries (world_id, name, content, entry_layer, overlay_action, supersedes_entry_id,"
            " created_at, updated_at) VALUES ('w1', 'Alpha', 'collapsed', 'dynamic', 'replace', 1, 't', 't')"
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")

        conn.execute("DELETE FROM lorebook_entries WHERE id = 1")
        conn.commit()

        assert conn.execute("SELECT id, content, overlay_action, supersedes_entry_id FROM lorebook_entries").fetchall() == [
            (2, "body", "", None),
            (3, "collapsed", "replace", None),
        ]
    finally:
        conn.close()


async def test_existing_database_migrates_before_latest_schema_indexes_run(tmp_path, monkeypatch):
    """A real startup must add ``entry_layer`` before init_db creates its index.

    This is the upgrade shape that previously crashed with
    ``OperationalError: no such column: entry_layer`` before 0053 could run.
    """
    path = tmp_path / "pre-0053.db"
    conn = _seeded(path)
    try:
        conn.execute("CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.executemany(
            "INSERT INTO schema_migrations (id) VALUES (?)",
            [(name,) for name in MIGRATIONS if name < "0053_dynamic_worlds"],
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
        assert conn.execute("SELECT COUNT(*) FROM lorebook_entries").fetchone() == (2,)
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
