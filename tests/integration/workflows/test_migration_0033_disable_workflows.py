"""Tests for migration 0033_disable_workflows.

Adds the two workflow-toggle columns idempotently and carries a prior format_consistency disable from its retired config flag
into the new workflow_enabled map, dropping the stale config key. Synchronous sqlite3, like the runner.
"""

import importlib
import json
import sqlite3
from contextlib import closing

import pytest

import backend.database.connection as db_connection


def _migrate(conn: sqlite3.Connection) -> None:
    importlib.import_module("backend.database.migrations.0033_disable_workflows").migrate(conn)


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


@pytest.fixture
def mig_db(tmp_path, monkeypatch):
    path = tmp_path / "mig.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    return path


def _stage_pre_0033(conn: sqlite3.Connection, *, workflow_config: str = "{}") -> None:
    # A pre-0033 settings row: workflow_config exists (added by 0020), but the two toggle columns do not.
    conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id=1), workflow_config TEXT NOT NULL DEFAULT '{}')")
    conn.execute("INSERT INTO settings (id, workflow_config) VALUES (1, ?)", (workflow_config,))
    conn.commit()


def test_adds_columns_with_defaults(mig_db):
    with closing(sqlite3.connect(str(mig_db))) as conn:
        _stage_pre_0033(conn)
        _migrate(conn)
        conn.commit()

        assert {"workflows_globally_enabled", "workflow_enabled"}.issubset(_cols(conn, "settings"))
        row = conn.execute("SELECT workflows_globally_enabled, workflow_enabled FROM settings WHERE id=1").fetchone()
        assert row == (1, "{}")


def test_carries_prior_format_consistency_disable(mig_db):
    with closing(sqlite3.connect(str(mig_db))) as conn:
        _stage_pre_0033(conn, workflow_config=json.dumps({"format_consistency": {"enabled": False}}))
        _migrate(conn)
        conn.commit()

        we = json.loads(conn.execute("SELECT workflow_enabled FROM settings WHERE id=1").fetchone()[0])
        assert we == {"format_consistency": False}
        # The retired config flag is removed (its empty parent slot may remain).
        wc = json.loads(conn.execute("SELECT workflow_config FROM settings WHERE id=1").fetchone()[0])
        assert "enabled" not in wc.get("format_consistency", {})


def test_does_not_carry_when_flag_true_or_absent(mig_db):
    with closing(sqlite3.connect(str(mig_db))) as conn:
        _stage_pre_0033(conn, workflow_config=json.dumps({"format_consistency": {"enabled": True}}))
        _migrate(conn)
        conn.commit()
        assert json.loads(conn.execute("SELECT workflow_enabled FROM settings WHERE id=1").fetchone()[0]) == {}


def test_idempotent_rerun(mig_db):
    with closing(sqlite3.connect(str(mig_db))) as conn:
        _stage_pre_0033(conn, workflow_config=json.dumps({"format_consistency": {"enabled": False}}))
        _migrate(conn)
        conn.commit()
        _migrate(conn)  # second pass: must not raise and must not change state
        conn.commit()
        assert json.loads(conn.execute("SELECT workflow_enabled FROM settings WHERE id=1").fetchone()[0]) == {
            "format_consistency": False
        }
