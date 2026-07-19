"""Fresh installs are *stamped* past the migration chain (see api lifespan), so
``schema.py`` + ``bootstrap`` must always equal what the migrations would have
produced. This is the gate that catches a migration whose schema/data change was
not mirrored into ``schema.py``/``seeds.py`` -- without it, fresh installs would
silently diverge from upgraded ones.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import backend.database.connection as db_connection
from backend.database import init_db
from backend.database.migrations import MIGRATIONS, run_pending, stamp_all

# Timestamps differ between the two builds; applied_at lives in schema_migrations.
_TIMESTAMP_COLS = {"applied_at", "created_at", "updated_at"}


def _snapshot(path: Path):
    conn = sqlite3.connect(path)
    try:
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
    finally:
        conn.close()


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
    if stamp:
        stamp_all(path)
    else:
        run_pending(path)


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

    # settings: migration 0020 ports legacy TTS columns into workflow_config as
    # {"tts": {auto_play: false, volume: 0.75}}; a stamped fresh install keeps the
    # empty '{}' slot, which get_workflow_config resolves to the tts workflow's
    # config_defaults carrying those same values. Everything else must match.
    def settings_row(path: Path) -> dict:
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            d = dict(conn.execute("SELECT * FROM settings WHERE id = 1").fetchone())
        finally:
            conn.close()
        return {k: _norm_json(v) for k, v in d.items() if k != "workflow_config"}

    assert settings_row(stamped) == settings_row(migrated)

    from backend.workflows.tts import tts_workflow

    ported = json.loads(sqlite3.connect(migrated).execute("SELECT workflow_config FROM settings").fetchone()[0]).get("tts", {})
    for key, val in ported.items():
        assert tts_workflow.config_defaults.get(key) == val, (
            f"0020 ports tts.{key}={val!r} but the workflow default is "
            f"{tts_workflow.config_defaults.get(key)!r} -- stamped fresh installs would differ"
        )


async def test_stamp_all_records_every_migration(tmp_path: Path, monkeypatch):
    path = tmp_path / "stamped.db"
    await _build(path, monkeypatch, stamp=True)
    conn = sqlite3.connect(path)
    try:
        stamped = {r[0] for r in conn.execute("SELECT id FROM schema_migrations")}
    finally:
        conn.close()
    assert stamped == set(MIGRATIONS)
