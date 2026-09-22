"""Upgrade coverage for the decision-fragment schema.

The interesting half is the cleanup: ``decision_default`` and
``decision_facets`` were both built on this branch and cut before release, so a
development database can carry either and neither may survive the upgrade.
"""

from __future__ import annotations

import importlib
import sqlite3

from backend.database import schema

_MIGRATION = importlib.import_module("backend.database.migrations.0066_decision_fragments")


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]  # nosec B608 -- literal table names


def _pre_0066(*, legacy: tuple[str, ...] = ()) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE interactive_fragments (
            id TEXT PRIMARY KEY,
            label TEXT,
            description TEXT,
            field_type TEXT,
            required INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            injection_label TEXT,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE messages (id INTEGER PRIMARY KEY);
        INSERT INTO messages VALUES (1);
        """
    )
    conn.executescript(schema.table_create_sql("settings"))
    for column in legacy:
        conn.execute(f"ALTER TABLE interactive_fragments ADD COLUMN {column} TEXT DEFAULT NULL")  # nosec B608 -- test literals
    conn.commit()
    return conn


def test_adds_the_decision_columns_and_seeds_the_outcome_fragment():
    conn = _pre_0066()
    _MIGRATION.migrate(conn)

    columns = _columns(conn, "interactive_fragments")
    assert "decision_type" in columns
    assert "decision_confidence_floor" in columns
    assert conn.execute("SELECT enabled FROM interactive_fragments WHERE id = 'outcome'").fetchone() == (0,)
    assert conn.execute("SELECT decision_evaluations FROM messages").fetchone() == ("{}",)


def test_the_facet_column_is_never_created():
    conn = _pre_0066()
    _MIGRATION.migrate(conn)

    assert "decision_facets" not in _columns(conn, "interactive_fragments")


def test_columns_cut_before_release_are_dropped_from_a_development_database():
    conn = _pre_0066(legacy=("decision_default", "decision_facets"))
    _MIGRATION.migrate(conn)

    columns = _columns(conn, "interactive_fragments")
    assert "decision_default" not in columns
    assert "decision_facets" not in columns


def test_migration_is_idempotent():
    conn = _pre_0066(legacy=("decision_facets",))
    _MIGRATION.migrate(conn)
    _MIGRATION.migrate(conn)

    assert _columns(conn, "interactive_fragments").count("decision_type") == 1
    assert conn.execute("SELECT count(*) FROM interactive_fragments WHERE id = 'outcome'").fetchone() == (1,)
