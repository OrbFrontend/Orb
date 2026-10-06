"""Migration 0079 drops the settings row's connection, model and sampler columns, also from a backup made before it."""

import importlib
import sqlite3
from contextlib import closing

import pytest

from backend.database import get_settings
from backend.database.schema import table_create_sql

_NAME = "0079_drop_legacy_storage"
_migration = importlib.import_module(f"backend.database.migrations.{_NAME}")

# The columns as a pre-0079 settings row carries them, holding values no current endpoint or model config has.
_LEGACY = (
    "endpoint_url TEXT NOT NULL DEFAULT 'http://stale/v1'",
    "api_key TEXT NOT NULL DEFAULT 'sk-stale'",
    "model_name TEXT NOT NULL DEFAULT 'stale-model'",
    "temperature REAL NOT NULL DEFAULT 1.7",
    "min_p REAL NOT NULL DEFAULT 0.05",
    "top_k INTEGER NOT NULL DEFAULT 7",
    "top_p REAL NOT NULL DEFAULT 0.5",
    "repetition_penalty REAL NOT NULL DEFAULT 1.3",
    "max_tokens INTEGER NOT NULL DEFAULT 99",
)
_LEGACY_NAMES = {declaration.split()[0] for declaration in _LEGACY}


def _add_legacy_columns(conn: sqlite3.Connection) -> None:
    for declaration in _LEGACY:
        conn.execute(f"ALTER TABLE settings ADD COLUMN {declaration}")  # nosec B608 -- test literals
    # Store the values in the row, as an old file has them. An ALTER-added REAL default is only implied, and quick_check, which
    # an import runs first, reports it as NULL.
    conn.execute("UPDATE settings SET temperature = temperature")


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(settings)")}


def test_drops_the_columns_keeps_the_row_and_reruns_as_a_no_op():
    conn = sqlite3.connect(":memory:")
    conn.execute(table_create_sql("settings"))
    _add_legacy_columns(conn)
    conn.execute("INSERT INTO settings (id, system_prompt, user_name) VALUES (1, 'model prompt', 'Ada')")

    _migration.migrate(conn)
    _migration.migrate(conn)

    assert not _columns(conn) & _LEGACY_NAMES
    assert conn.execute("SELECT system_prompt, user_name FROM settings").fetchone() == ("model prompt", "Ada")


@pytest.mark.parametrize("action", ["apply", "restore"])
async def test_a_backup_carrying_the_columns_still_imports(client, db_path, action):
    from backend.features.presets import ALL_DOMAINS

    before = await get_settings()
    export = {"domains": list(ALL_DOMAINS), "strip_keys": False}
    name = (await client.post_json("/api/presets/export", json=export))["name"]
    with closing(sqlite3.connect(str(db_path.parent / "snapshots" / name))) as backup:
        _add_legacy_columns(backup)
        backup.execute("DELETE FROM schema_migrations WHERE id = ?", (_NAME,))
        backup.commit()

    await client.post_checked(f"/api/presets/{name}/{action}", json={})

    with closing(sqlite3.connect(str(db_path))) as live:
        assert not _columns(live) & _LEGACY_NAMES
    after = await get_settings()
    overlaid = ("endpoint_url", "api_key", "model_name", "temperature", "top_k", "max_tokens")
    assert {key: after[key] for key in overlaid} == {key: before[key] for key in overlaid}
