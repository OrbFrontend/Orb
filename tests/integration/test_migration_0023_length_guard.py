"""Tests for migration 0023_separate_length_guard_flags.

0023 promotes the length-guard feature flags out of the enabled_tools JSON into
their own boolean columns. These pin the two contracts: existing values are
ported into the new columns, and both keys are stripped from enabled_tools while
the real tool entries are left untouched. They use sqlite3 directly -- the
runner is synchronous and takes a connection.
"""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest


def _migrate(conn: sqlite3.Connection) -> None:
    importlib.import_module("backend.database.migrations.0023_separate_length_guard_flags").migrate(conn)


def _cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(settings)").fetchall()}


@pytest.fixture
def conn() -> sqlite3.Connection:
    """A pre-0023 settings table: enabled_tools exists, the new columns don't."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id=1), " "enabled_tools TEXT NOT NULL DEFAULT '{}')")
    yield c
    c.close()


def _enabled_tools(conn: sqlite3.Connection) -> dict:
    return json.loads(conn.execute("SELECT enabled_tools FROM settings WHERE id=1").fetchone()[0])


def _flags(conn: sqlite3.Connection) -> tuple[int, int]:
    return conn.execute("SELECT length_guard_enabled, length_guard_enforce FROM settings WHERE id=1").fetchone()


def test_adds_columns(conn: sqlite3.Connection):
    conn.execute("INSERT INTO settings (id, enabled_tools) VALUES (1, '{}')")
    _migrate(conn)
    assert {"length_guard_enabled", "length_guard_enforce"} <= _cols(conn)


def test_ports_flags_and_strips_keys(conn: sqlite3.Connection):
    conn.execute(
        "INSERT INTO settings (id, enabled_tools) VALUES (1, ?)",
        (json.dumps({"direct_scene": True, "length_guard": True, "length_guard_enforce": True}),),
    )
    _migrate(conn)

    assert _flags(conn) == (1, 1)
    tools = _enabled_tools(conn)
    assert "length_guard" not in tools
    assert "length_guard_enforce" not in tools
    assert tools == {"direct_scene": True}  # real tool entries survive


def test_length_guard_on_enforce_off(conn: sqlite3.Connection):
    conn.execute(
        "INSERT INTO settings (id, enabled_tools) VALUES (1, ?)",
        (json.dumps({"length_guard": True}),),
    )
    _migrate(conn)
    assert _flags(conn) == (1, 0)


def test_no_length_guard_keys_defaults_to_off(conn: sqlite3.Connection):
    conn.execute(
        "INSERT INTO settings (id, enabled_tools) VALUES (1, ?)",
        (json.dumps({"direct_scene": True, "editor_apply_patch": False}),),
    )
    _migrate(conn)
    assert _flags(conn) == (0, 0)
    assert _enabled_tools(conn) == {"direct_scene": True, "editor_apply_patch": False}


def test_idempotent_on_rerun(conn: sqlite3.Connection):
    conn.execute(
        "INSERT INTO settings (id, enabled_tools) VALUES (1, ?)",
        (json.dumps({"length_guard": True, "length_guard_enforce": True}),),
    )
    _migrate(conn)
    _migrate(conn)  # column-add is guarded; second run must not raise
    assert _flags(conn) == (1, 1)
