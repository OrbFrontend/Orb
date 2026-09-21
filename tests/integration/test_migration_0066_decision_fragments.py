"""Upgrade coverage for the decision-fragment columns."""

from __future__ import annotations

import importlib
import sqlite3

_MIGRATION = importlib.import_module("backend.database.migrations.0066_decision_fragments")

_FRAGMENT_COLUMNS = (
    "decision_type",
    "decision_placement",
    "decision_state_template",
    "decision_instructions",
    "decision_criteria",
    "decision_outputs",
    "decision_default",
    "decision_resolution",
    "decision_threshold",
)


def _pre_0066() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE interactive_fragments (id TEXT PRIMARY KEY, field_type TEXT NOT NULL DEFAULT 'string');
        CREATE TABLE messages (id INTEGER PRIMARY KEY);
        CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id = 1));
        INSERT INTO interactive_fragments (id) VALUES ('pacing');
        INSERT INTO messages VALUES (1);
        INSERT INTO settings VALUES (1);
        """
    )
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_adds_authoring_columns_nullable_for_existing_fragments():
    conn = _pre_0066()
    _MIGRATION.migrate(conn)

    row = conn.execute(f"SELECT {', '.join(_FRAGMENT_COLUMNS)} FROM interactive_fragments").fetchone()
    # An upgraded database has no decisions and behaves exactly as before.
    assert row == (None,) * len(_FRAGMENT_COLUMNS)


def test_adds_per_reply_snapshots_defaulting_to_empty():
    conn = _pre_0066()
    _MIGRATION.migrate(conn)

    assert conn.execute("SELECT decision_evaluations, decision_cooldowns FROM messages").fetchone() == ("{}", "{}")


def test_adds_classifier_configuration_unconfigured_by_default():
    conn = _pre_0066()
    _MIGRATION.migrate(conn)

    row = conn.execute(
        "SELECT decision_endpoint_id, decision_model, decision_url, decision_config_revision, decision_card_approvals "
        "FROM settings"
    ).fetchone()
    # No endpoint means enabled decisions use their authored fallback and make no
    # request -- the state a fresh install also starts in.
    assert row == (None, "typesafe/jev-1.13", "", 0, "{}")


def test_migration_is_idempotent():
    conn = _pre_0066()
    _MIGRATION.migrate(conn)
    _MIGRATION.migrate(conn)

    assert _columns(conn, "messages").count("decision_evaluations") == 1
    assert _columns(conn, "interactive_fragments").count("decision_type") == 1
    assert _columns(conn, "settings").count("decision_card_approvals") == 1


def test_fresh_schema_and_migrated_schema_agree_on_the_new_columns():
    from backend.database.schema import CREATE_TABLES_SQL

    fresh = sqlite3.connect(":memory:")
    fresh.executescript(CREATE_TABLES_SQL)
    migrated = _pre_0066()
    _MIGRATION.migrate(migrated)

    for table, columns in (
        ("interactive_fragments", _FRAGMENT_COLUMNS),
        ("messages", ("decision_evaluations", "decision_cooldowns")),
        (
            "settings",
            (
                "decision_endpoint_id",
                "decision_model",
                "decision_url",
                "decision_config_revision",
                "decision_card_approvals",
            ),
        ),
    ):
        fresh_columns = set(_columns(fresh, table))
        migrated_columns = set(_columns(migrated, table))
        for column in columns:
            assert column in fresh_columns, (table, column)
            assert column in migrated_columns, (table, column)
