"""Migration 0071 backfills only an absent `negated_narration` audit toggle."""

import importlib
import json
import sqlite3

from backend.database.seeds import DEFAULT_SETTINGS

_migration = importlib.import_module("backend.database.migrations.0071_negated_narration_audit_toggle")

_LEGACY = {"banned_phrases": True, "anti_echo": True}


def _db(*maps: dict) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, editor_audit_toggles TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO settings (id, editor_audit_toggles) VALUES (?, ?)", [(i, json.dumps(m)) for i, m in enumerate(maps, 1)]
    )
    return conn


def _toggles(conn: sqlite3.Connection) -> list[dict]:
    return [json.loads(row[0]) for row in conn.execute("SELECT editor_audit_toggles FROM settings ORDER BY id")]


def test_backfills_absent_key_and_preserves_explicit_choices():
    conn = _db(_LEGACY, {**_LEGACY, "negated_narration": True}, {**_LEGACY, "negated_narration": False})
    _migration.migrate(conn)
    after = _toggles(conn)
    assert after[0] == {**_LEGACY, "negated_narration": False}
    assert after[1]["negated_narration"] is True
    assert after[2]["negated_narration"] is False


def test_rerun_is_idempotent():
    conn = _db(_LEGACY, {**_LEGACY, "negated_narration": True})
    _migration.migrate(conn)
    once = _toggles(conn)
    _migration.migrate(conn)
    assert _toggles(conn) == once


def test_missing_column_is_a_no_op():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY)")
    _migration.migrate(conn)


def test_fresh_defaults_carry_the_release_value():
    assert DEFAULT_SETTINGS["editor_audit_toggles"]["negated_narration"] is False
