"""Fresh installs are *stamped* past the migration chain by ``init_db``, so ``schema.py`` + ``bootstrap`` must always equal what
the migrations would have produced. This is the gate that catches a migration whose schema/data change was not mirrored into
``schema.py``/``seeds.py`` -- without it, fresh installs would silently diverge from upgraded ones.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

import backend.api as api_module
import backend.database.bootstrap as bootstrap
import backend.database.connection as db_connection
import backend.database.migrations as migrations
from backend.database import init_db
from backend.database.migrations import MIGRATIONS, run_pending

# Timestamps differ between the two builds; applied_at lives in schema_migrations.
_TIMESTAMP_COLS = {"applied_at", "created_at", "updated_at"}


def _snapshot(path: Path):
    with closing(sqlite3.connect(path)) as conn:
        schema = sorted(
            (r[0], r[1], r[2] or "")
            for r in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        )
        rows = {}
        for t in [n for k, n, _ in schema if k == "table"]:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
            keep = [c for c in cols if c not in _TIMESTAMP_COLS]
            table_rows = []
            for row in conn.execute(f"SELECT {', '.join(keep) or '1'} FROM {t}"):
                table_rows.append(str(sorted(zip(keep, map(_norm_json, row)))))
            rows[t] = sorted(table_rows)
        return schema, rows


def _norm_json(v):
    """Compare JSON columns semantically -- migrations may re-serialize with
    different whitespace/key order than the schema defaults."""
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except ValueError:
            return v
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, sort_keys=True)
    return v


async def _build(path: Path, monkeypatch, *, stamp: bool) -> None:
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    await init_db()
    if not stamp:
        # Only this equivalence test deliberately replays history against a
        # current schema. Initialization itself must never do so.
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("DELETE FROM schema_migrations")
            conn.commit()
        assert run_pending(path) == len(MIGRATIONS)


async def test_stamped_fresh_db_equals_migrated_fresh_db(tmp_path: Path, monkeypatch):
    stamped, migrated = tmp_path / "stamped.db", tmp_path / "migrated.db"
    await _build(stamped, monkeypatch, stamp=True)
    await _build(migrated, monkeypatch, stamp=False)

    s_schema, s_rows = _snapshot(stamped)
    m_schema, m_rows = _snapshot(migrated)

    assert s_schema == m_schema, "fresh-install schema diverges from migrated schema:\n" + "\n".join(
        str(x) for x in sorted(set(s_schema) ^ set(m_schema))
    )
    for t in s_rows:
        if t == "settings":
            continue  # workflow_config handled below
        assert s_rows[t] == m_rows[t], f"seed rows diverge in {t!r}"

    # settings: migration 0020 ports legacy TTS columns into workflow_config as {"tts": {auto_play: false, volume: 0.75}}; a
    # stamped fresh install keeps the empty '{}' slot, which get_workflow_config resolves to the tts workflow's config_defaults
    # carrying those same values. Everything else must match.
    def settings_row(path: Path) -> dict:
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            d = dict(conn.execute("SELECT * FROM settings WHERE id = 1").fetchone())
        return {k: _norm_json(v) for k, v in d.items() if k != "workflow_config"}

    assert settings_row(stamped) == settings_row(migrated)

    from backend.workflows.tts import WORKFLOW as tts_workflow

    ported = json.loads(sqlite3.connect(migrated).execute("SELECT workflow_config FROM settings").fetchone()[0]).get("tts", {})
    for key, val in ported.items():
        assert tts_workflow.config_defaults.get(key) == val, (
            f"0020 ports tts.{key}={val!r} but the workflow default is "
            f"{tts_workflow.config_defaults.get(key)!r} -- stamped fresh installs would differ"
        )


def _assert_current(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        assert {r[0] for r in conn.execute("SELECT id FROM schema_migrations")} == set(MIGRATIONS)
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("file_state", ["missing", "zero_bytes", "sqlite_header", "internal_table"])
async def test_fresh_startup_never_runs_migrations(tmp_path: Path, monkeypatch, file_state):
    path = tmp_path / "fresh.db"
    if file_state == "zero_bytes":
        path.touch()
    elif file_state in {"sqlite_header", "internal_table"}:
        with closing(sqlite3.connect(path)) as conn:
            if file_state == "internal_table":
                # Dropping the last application table leaves sqlite_sequence.
                conn.executescript("CREATE TABLE scratch (id INTEGER PRIMARY KEY AUTOINCREMENT); DROP TABLE scratch;")
            else:
                conn.execute("PRAGMA user_version = 1")
        assert path.stat().st_size > 0

    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    runner = Mock(side_effect=AssertionError("fresh installs must bypass the migration runner"))
    monkeypatch.setattr(bootstrap, "run_pending", runner)
    real_import = migrations.importlib.import_module

    def no_migration_import(name, *args, **kwargs):
        assert not name.startswith("backend.database.migrations."), name
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(migrations.importlib, "import_module", no_migration_import)
    async with api_module.lifespan(FastAPI()):
        _assert_current(path)
    runner.assert_not_called()

    # Later starts may check the ledger, but must not import/replay any migration.
    monkeypatch.setattr(bootstrap, "run_pending", run_pending)
    before = _snapshot(path)
    async with api_module.lifespan(FastAPI()):
        _assert_current(path)
    assert _snapshot(path) == before


@pytest.mark.parametrize("failure_stage", ["schema", "seeds", "stamp"])
async def test_failed_fresh_initialization_rolls_back_and_retries_without_migrations(
    tmp_path: Path, monkeypatch, failure_stage
):
    path = tmp_path / "retry.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    runner = Mock(side_effect=AssertionError("failed fresh installs must still bypass migrations"))
    monkeypatch.setattr(bootstrap, "run_pending", runner)

    with monkeypatch.context() as fault:
        if failure_stage == "schema":
            fault.setattr(bootstrap, "CREATE_TABLES_SQL", bootstrap.CREATE_TABLES_SQL + "\nINVALID SQL;")
        else:
            target = "_seed_phrase_bank" if failure_stage == "seeds" else "stamp_all"
            original = getattr(bootstrap, target)

            async def fail_after_writes(db):
                await original(db)
                raise RuntimeError("interrupted initialization")

            fault.setattr(bootstrap, target, fail_after_writes)
        with pytest.raises(sqlite3.OperationalError if failure_stage == "schema" else RuntimeError):
            await init_db()

    # Neither partial schema/data nor premature migration stamps survive.
    assert _snapshot(path) == ([], {})
    assert await init_db() == 0
    _assert_current(path)
    runner.assert_not_called()


async def test_fresh_baseline_still_runs_future_migrations_once(tmp_path: Path, monkeypatch):
    path = tmp_path / "future.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    assert await init_db() == 0
    _assert_current(path)

    name = "9999_test_future_upgrade"
    monkeypatch.setattr(migrations, "MIGRATIONS", [*MIGRATIONS, name])

    def upgrade(conn):
        conn.execute("UPDATE settings SET user_description = 'upgraded after fresh install' WHERE id = 1")

    importer = Mock(return_value=SimpleNamespace(migrate=upgrade))
    monkeypatch.setattr(migrations.importlib, "import_module", importer)
    assert await init_db() == 1
    assert await init_db() == 0
    importer.assert_called_once_with(f"backend.database.migrations.{name}")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT user_description FROM settings").fetchone() == ("upgraded after fresh install",)
        assert {r[0] for r in conn.execute("SELECT id FROM schema_migrations")} == {*MIGRATIONS, name}
