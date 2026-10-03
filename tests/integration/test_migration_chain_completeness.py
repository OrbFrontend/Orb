"""The migration chain must reach the current ``schema.py``, not just agree with it.

``test_fresh_install_stamping`` builds *both* of its databases from ``schema.py``
and then runs the chain over one of them. Migrations are idempotent against a
column that already exists, so that test can only catch drift in one direction:
a migration whose change was never mirrored into ``schema.py``.

The other direction is invisible to it, and it is the direction that breaks
users: a column added to ``schema.py`` with no migration to add it. Fresh
installs read ``schema.py`` and look fine; every *upgrading* install runs the
chain instead and ends up without the column, so the first query naming it fails
with ``no such column``.

This test closes that side by starting from a frozen historical schema — what an
installed database actually looked like before the current work — and asserting
the chain carries it all the way to today's ``schema.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import backend.database.connection as db_connection
from backend.database import init_db
from backend.database.migrations import MIGRATIONS, run_pending

_BASELINE = Path(__file__).parent.parent / "fixtures" / "schema_pre_group_chats.sql"


def _columns(path: Path) -> dict[str, set[str]]:
    """``{table: {column, ...}}`` for every non-internal table."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: {c[1] for c in conn.execute(f"PRAGMA table_info({t})")} for t in tables}  # nosec B608 -- from sqlite_master
    finally:
        conn.close()


async def test_migration_chain_reaches_current_schema(tmp_path: Path, monkeypatch):
    upgraded = tmp_path / "upgraded.db"
    conn = sqlite3.connect(upgraded)
    try:
        conn.executescript(_BASELINE.read_text())
        conn.commit()
    finally:
        conn.close()
    run_pending(upgraded)

    fresh = tmp_path / "fresh.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(fresh))
    await init_db()

    upgraded_cols, fresh_cols = _columns(upgraded), _columns(fresh)

    missing_tables = set(fresh_cols) - set(upgraded_cols) - {"schema_migrations"}
    assert not missing_tables, (
        f"tables in schema.py that the migration chain never creates: {sorted(missing_tables)} — "
        "upgrading installs would not have them"
    )

    missing_columns = {
        table: sorted(fresh_cols[table] - upgraded_cols[table])
        for table in fresh_cols
        if table in upgraded_cols and fresh_cols[table] - upgraded_cols[table]
    }
    assert not missing_columns, (
        f"columns in schema.py that the migration chain never adds: {missing_columns} — "
        "fresh installs get them from schema.py, but every upgrading install would fail "
        "the first query that names one. Add them to a migration."
    )

    leftover_tables = set(upgraded_cols) - set(fresh_cols)
    leftover_columns = {
        table: sorted(upgraded_cols[table] - fresh_cols[table])
        for table in upgraded_cols
        if table in fresh_cols and upgraded_cols[table] - fresh_cols[table]
    }
    assert not leftover_tables and not leftover_columns, (
        f"schema the migration chain leaves behind that schema.py no longer has: tables {sorted(leftover_tables)}, "
        f"columns {leftover_columns} — upgraded installs would carry storage fresh ones lack. Drop it in a migration."
    )


def test_settings_rebuild_keeps_what_later_migrations_read(tmp_path: Path):
    """0066 rebuilds ``settings`` from today's DDL; 0067 and 0068 still read columns that DDL lacks."""
    upgraded = tmp_path / "upgraded.db"
    conn = sqlite3.connect(upgraded)
    try:
        conn.executescript(_BASELINE.read_text())
        conn.execute(
            "INSERT INTO settings (id, endpoint_url, model_name, feedback_enabled, direction_notes_record, "
            "direction_notes_inject) VALUES (1, 'u', 'm', 0, 1, 'writer')"
        )
        conn.execute(
            "INSERT INTO interactive_fragments (id, label, description, field_type, injection_label) "
            "VALUES ('fb', 'FB', 'd', 'feedback', 'FB')"
        )
        conn.commit()
    finally:
        conn.close()
    run_pending(upgraded)

    conn = sqlite3.connect(upgraded)
    try:
        feedback = conn.execute("SELECT enabled FROM interactive_fragments WHERE id = 'fb'").fetchone()
        notes = conn.execute(
            "SELECT field_type, state_update, state_inject FROM interactive_fragments WHERE id = 'characterization'"
        ).fetchone()
    finally:
        conn.close()
    assert feedback == (0,), "0068 must keep feedback fragments off for an install that had feedback off"
    assert notes == ("state", "after_reply", "writer"), "0067 must convert direction notes with the recorded settings"


@pytest.mark.parametrize("populated", [False, True])
async def test_initialization_upgrades_unstamped_existing_schema(tmp_path: Path, monkeypatch, populated):
    """Empty tables or a missing ledger do not make an old schema fresh."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_BASELINE.read_text())
        if populated:
            conn.execute(
                "INSERT INTO settings (id, endpoint_url, model_name, user_name, user_description) "
                "VALUES (1, 'http://legacy/v1', 'legacy-model', 'Legacy User', 'Keep my persona')"
            )
            conn.execute("INSERT INTO worlds (id, name, created_at, updated_at) VALUES ('legacy', 'Keep my world', 't', 't')")
            conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    assert await init_db() == len(MIGRATIONS)
    assert await init_db() == 0
    conn = sqlite3.connect(path)
    try:
        assert {r[0] for r in conn.execute("SELECT id FROM schema_migrations")} == set(MIGRATIONS)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        if populated:
            # 0003 must actually backfill the user's persona, not get stamped
            # away merely because this database has no migration ledger.
            assert conn.execute(
                "SELECT p.name, p.description FROM user_personas p JOIN settings s ON p.id = s.active_persona_id"
            ).fetchone() == ("Legacy User", "Keep my persona")
            assert conn.execute("SELECT name FROM worlds WHERE id = 'legacy'").fetchone() == ("Keep my world",)
    finally:
        conn.close()
