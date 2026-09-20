"""Upgrade coverage for nullable endpoint model parameters."""

from __future__ import annotations

import importlib
import sqlite3

_MIGRATION = importlib.import_module("backend.database.migrations.0064_nullable_model_hyperparams")


def _pre_0064() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE endpoints (
            id INTEGER PRIMARY KEY,
            active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL,
            agent_active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL
        );
        CREATE TABLE model_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
            model_name TEXT NOT NULL,
            system_prompt TEXT NOT NULL DEFAULT '',
            temperature REAL NOT NULL DEFAULT 0.8,
            min_p REAL NOT NULL DEFAULT 0.0,
            top_k INTEGER NOT NULL DEFAULT 40,
            top_p REAL NOT NULL DEFAULT 0.95,
            repetition_penalty REAL NOT NULL DEFAULT 1.0,
            max_tokens INTEGER NOT NULL DEFAULT 4096,
            role TEXT NOT NULL DEFAULT 'writer' CHECK (role IN ('writer', 'agent')),
            reasoning_effort TEXT NOT NULL DEFAULT '',
            reasoning_effort_param TEXT NOT NULL DEFAULT '',
            reasoning_effort_value TEXT NOT NULL DEFAULT '',
            extra_headers TEXT NOT NULL DEFAULT '',
            extra_body TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO endpoints (id) VALUES (1);
        INSERT INTO model_configs (id, endpoint_id, model_name) VALUES (7, 1, 'existing');
        UPDATE endpoints SET active_model_config_id = 7 WHERE id = 1;
        """
    )
    return conn


def test_rebuild_preserves_active_model_references_and_accepts_nulls():
    conn = _pre_0064()
    try:
        _MIGRATION.migrate(conn)
        conn.execute("UPDATE model_configs SET temperature = NULL, max_tokens = NULL WHERE id = 7")

        assert conn.execute("SELECT active_model_config_id FROM endpoints WHERE id = 1").fetchone() == (7,)
        assert conn.execute("SELECT temperature, max_tokens FROM model_configs WHERE id = 7").fetchone() == (None, None)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_migration_is_idempotent_after_the_rebuild():
    conn = _pre_0064()
    try:
        _MIGRATION.migrate(conn)
        _MIGRATION.migrate(conn)
        assert conn.execute("PRAGMA table_info(model_configs)").fetchall()[4][3] == 0
    finally:
        conn.close()
