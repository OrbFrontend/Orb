"""Tests for migration 0020_workflows (the squashed workflow migration).

0020_workflows collapses the feature's seven development migrations
into one net 0019 -> final-shape delta. These pin the squash's two contracts:
the final schema it converges on, and the legacy data it ports on a real
upgrade (user attachments copied, TTS config reshaped, voice profiles moved
per-card with the vestigial endpoint_id dropped). They use sqlite3 directly --
the runner is synchronous and takes a connection.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest

import backend.database.connection as db_connection
from backend.database.migrations import run_pending


def _migrate(conn: sqlite3.Connection) -> None:
    importlib.import_module("backend.database.migrations.0020_workflows").migrate(conn)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


@pytest.fixture
def mig_db(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "mig.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    return path


def _stage_upgrade(conn: sqlite3.Connection) -> None:
    """A post-PR-pull, pre-0020 DB with legacy data. message_attachments has no
    `source` column (old 0021 never ran), settings carries tts_* but not
    workflow_config, and the per-scope workflow_state columns are absent -- the
    real shape an existing main install presents when the workflow PR lands."""
    conn.executescript(
        """
        CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id=1), endpoint_url TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '', tts_enabled INTEGER NOT NULL DEFAULT 0,
            tts_auto_speak INTEGER NOT NULL DEFAULT 0, tts_volume REAL NOT NULL DEFAULT 0.75);
        CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, created_at TEXT NOT NULL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, turn_index INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE character_cards (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE message_attachments (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
            mime_type TEXT NOT NULL, data_b64 TEXT NOT NULL, filename TEXT, size INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE voice_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, character_card_id TEXT NOT NULL UNIQUE,
            backend TEXT NOT NULL DEFAULT 'edge', voice_id TEXT NOT NULL DEFAULT 'en-US-JennyNeural',
            language TEXT NOT NULL DEFAULT 'en-US', rate REAL NOT NULL DEFAULT 1.0, pitch REAL NOT NULL DEFAULT 1.0,
            enabled INTEGER NOT NULL DEFAULT 0, endpoint_id INTEGER, api_url TEXT DEFAULT '', api_key TEXT DEFAULT '',
            model TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
        """
    )
    conn.execute(
        "INSERT INTO settings (id, endpoint_url, model_name, tts_enabled, tts_auto_speak, tts_volume) VALUES (1,'','',1,1,0.42)"
    )
    conn.execute("INSERT INTO conversations (id, title, created_at) VALUES ('c1','t','2026-01-01T00:00:00Z')")
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, turn_index, created_at) VALUES (1,'c1','assistant','x',0,'2026-01-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO character_cards (id, name) VALUES ('card1','Alice')")
    # One user upload, and one attachment on a missing message (orphan, must not copy).
    conn.execute(
        "INSERT INTO message_attachments (id, message_id, mime_type, data_b64, filename, size, created_at) VALUES (10,1,'image/png','VVNFUg==','u.png',5,'2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO message_attachments (id, message_id, mime_type, data_b64, filename, size, created_at) VALUES (11,999,'image/png','T1JQSA==','orphan.png',5,'2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO voice_profiles (character_card_id, backend, voice_id, language, rate, pitch, enabled, endpoint_id, api_url, api_key, model) "
        "VALUES ('card1','edge','v1','en-US',1.2,0.9,1,7,'','','')"
    )
    conn.commit()


def test_upgrade_converges_on_final_schema(mig_db: Path):
    conn = sqlite3.connect(str(mig_db))
    try:
        _stage_upgrade(conn)
        _migrate(conn)
        conn.commit()

        tables = _tables(conn)
        assert "user_attachments" in tables
        assert "workflow_attachments" in tables
        assert "message_attachments" not in tables
        assert "voice_profiles" not in tables

        settings_cols = _cols(conn, "settings")
        assert {"workflow_config", "attachment_cache_budget_bytes", "attachment_access_counter"}.issubset(settings_cols)
        assert not any(c.startswith("tts_") for c in settings_cols)
        assert "workflow_state" in _cols(conn, "conversations")
        assert "workflow_state" in _cols(conn, "messages")
        assert "workflow_state" in _cols(conn, "character_cards")
    finally:
        conn.close()


def test_upgrade_copies_user_rows_skipping_orphans(mig_db: Path):
    conn = sqlite3.connect(str(mig_db))
    try:
        _stage_upgrade(conn)
        _migrate(conn)
        conn.commit()
        rows = conn.execute("SELECT id, message_id, mime_type, filename, size FROM user_attachments ORDER BY id").fetchall()
        assert rows == [(10, 1, "image/png", "u.png", 5)]
        assert conn.execute("SELECT COUNT(*) FROM workflow_attachments").fetchone()[0] == 0
    finally:
        conn.close()


def test_upgrade_ports_tts_to_final_shape(mig_db: Path):
    conn = sqlite3.connect(str(mig_db))
    try:
        _stage_upgrade(conn)
        _migrate(conn)
        conn.commit()

        wc = json.loads(conn.execute("SELECT workflow_config FROM settings WHERE id=1").fetchone()[0])
        assert wc["tts"] == {"auto_play": True, "volume": 0.42}

        card_state = json.loads(conn.execute("SELECT workflow_state FROM character_cards WHERE id='card1'").fetchone()[0])
        # Full dict: endpoint_id dropped, enabled coerced to bool, empty api_* preserved as "".
        assert card_state["tts"] == {
            "backend": "edge",
            "voice_id": "v1",
            "language": "en-US",
            "rate": 1.2,
            "pitch": 0.9,
            "enabled": True,
            "api_url": "",
            "api_key": "",
            "model": "",
        }
    finally:
        conn.close()


def test_tts_port_preserves_unrelated_workflow_config_keys(mig_db: Path):
    """The port reassigns only workflow_config["tts"]; another workflow's slot
    already present in the config must survive."""
    conn = sqlite3.connect(str(mig_db))
    try:
        _stage_upgrade(conn)
        conn.execute("ALTER TABLE settings ADD COLUMN workflow_config TEXT NOT NULL DEFAULT '{}'")
        conn.execute("UPDATE settings SET workflow_config = ? WHERE id=1", (json.dumps({"other_wf": {"k": 1}}),))
        conn.commit()

        _migrate(conn)
        conn.commit()

        wc = json.loads(conn.execute("SELECT workflow_config FROM settings WHERE id=1").fetchone()[0])
        assert wc["other_wf"] == {"k": 1}
        assert wc["tts"] == {"auto_play": True, "volume": 0.42}
    finally:
        conn.close()


def test_does_not_clobber_existing_card_tts_profile(mig_db: Path):
    conn = sqlite3.connect(str(mig_db))
    try:
        _stage_upgrade(conn)
        live = {"tts": {"backend": "openai", "voice_id": "nova", "enabled": False}}
        conn.execute("ALTER TABLE character_cards ADD COLUMN workflow_state TEXT DEFAULT NULL")
        conn.execute("UPDATE character_cards SET workflow_state=? WHERE id='card1'", (json.dumps(live),))
        conn.commit()

        _migrate(conn)
        conn.commit()

        card_state = json.loads(conn.execute("SELECT workflow_state FROM character_cards WHERE id='card1'").fetchone()[0])
        assert card_state["tts"] == {"backend": "openai", "voice_id": "nova", "enabled": False}
    finally:
        conn.close()


def test_idempotent_rerun(mig_db: Path):
    conn = sqlite3.connect(str(mig_db))
    try:
        _stage_upgrade(conn)
        _migrate(conn)
        conn.commit()
        first_wc = conn.execute("SELECT workflow_config FROM settings WHERE id=1").fetchone()[0]

        _migrate(conn)  # second pass must not raise and must not change state
        conn.commit()

        assert conn.execute("SELECT workflow_config FROM settings WHERE id=1").fetchone()[0] == first_wc
        assert "message_attachments" not in _tables(conn)
        assert conn.execute("SELECT id FROM user_attachments ORDER BY id").fetchall() == [(10,)]
    finally:
        conn.close()


def test_no_legacy_tts_leaves_config_empty(mig_db: Path):
    """A settings row with neither tts_* columns nor a voice_profiles table (a
    fresh-branch shape) gets workflow_config left untouched by the TTS port."""
    conn = sqlite3.connect(str(mig_db))
    try:
        conn.executescript(
            """
            CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id=1), workflow_config TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE conversations (id TEXT PRIMARY KEY);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE character_cards (id TEXT PRIMARY KEY);
            """
        )
        conn.execute("INSERT INTO settings (id, workflow_config) VALUES (1, '{}')")
        conn.commit()
        _migrate(conn)
        conn.commit()
        assert json.loads(conn.execute("SELECT workflow_config FROM settings WHERE id=1").fetchone()[0]) == {}
    finally:
        conn.close()


def test_run_pending_fires_unified_and_drops_message_attachments(mig_db: Path):
    """End-to-end through the real runner on a fresh branch DB: with 0001-0019
    marked applied, run_pending fires only 0020_workflows, which drops
    the message_attachments table schema.py keeps purely as migration-bootstrap
    scaffolding (migration 0002 deletes from it on a fresh boot)."""
    from backend.database.migrations import MIGRATIONS
    from backend.database.schema import CREATE_TABLES_SQL

    conn = sqlite3.connect(str(mig_db))
    try:
        conn.executescript(CREATE_TABLES_SQL)
        conn.execute("INSERT INTO settings (id, endpoint_url, model_name) VALUES (1, '', '')")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        for name in MIGRATIONS:
            if name != "0020_workflows":
                conn.execute("INSERT OR IGNORE INTO schema_migrations (id) VALUES (?)", (name,))
        conn.commit()
    finally:
        conn.close()

    run_pending(mig_db)

    conn = sqlite3.connect(str(mig_db))
    try:
        tables = _tables(conn)
        assert "message_attachments" not in tables
        assert {"user_attachments", "workflow_attachments"}.issubset(tables)
        applied = {r[0] for r in conn.execute("SELECT id FROM schema_migrations")}
        assert "0020_workflows" in applied
    finally:
        conn.close()
