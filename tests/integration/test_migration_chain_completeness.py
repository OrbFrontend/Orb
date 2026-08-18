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

import backend.database.connection as db_connection
from backend.database import init_db
from backend.database.migrations import run_pending

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
