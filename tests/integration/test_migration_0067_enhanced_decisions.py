from __future__ import annotations

import importlib
import sqlite3

_MIGRATION = importlib.import_module("backend.database.migrations.0067_enhanced_decision_fragments")


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE interactive_fragments ("
        "id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', "
        "field_type TEXT NOT NULL DEFAULT 'string', required INTEGER NOT NULL DEFAULT 0, "
        "enabled INTEGER NOT NULL DEFAULT 1, injection_label TEXT NOT NULL DEFAULT '', "
        "sort_order INTEGER NOT NULL DEFAULT 0, decision_type TEXT, decision_placement TEXT, "
        "decision_state_template TEXT, decision_instructions TEXT, decision_criteria TEXT, "
        "decision_outputs TEXT, decision_default TEXT, decision_resolution TEXT, decision_threshold REAL)"
    )
    return connection


def test_migration_adds_columns_and_a_disabled_primary_only_outcome_seed():
    connection = _database()
    _MIGRATION.migrate(connection)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(interactive_fragments)")}
    assert {"decision_facets", "decision_confidence_floor"} <= columns
    row = connection.execute(
        "SELECT enabled, decision_instructions, decision_resolution, decision_facets "
        "FROM interactive_fragments WHERE id = 'outcome'"
    ).fetchone()
    assert row == (0, "The action described in the current request succeeds.", "roll", None)


def test_migration_preserves_an_existing_outcome_and_is_idempotent():
    connection = _database()
    connection.execute("INSERT INTO interactive_fragments (id, label) VALUES ('outcome', 'Mine')")
    _MIGRATION.migrate(connection)
    _MIGRATION.migrate(connection)

    assert connection.execute("SELECT label FROM interactive_fragments WHERE id = 'outcome'").fetchone() == ("Mine",)
    assert connection.execute("SELECT COUNT(*) FROM interactive_fragments WHERE id = 'outcome'").fetchone() == (1,)
