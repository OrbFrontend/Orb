"""Migration 0073 moves character_cards.avatar_b64 to the end of the row.

A column stored after a large avatar is reachable only by walking the avatar's
overflow chain, so the library list paid for every avatar it never read.
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from backend.database import schema
from backend.database.migrations import run_pending

_migration = importlib.import_module("backend.database.migrations.0073_character_avatar_last")

_BASELINE = Path(__file__).parent.parent / "fixtures" / "schema_pre_group_chats.sql"


def _legacy_schema() -> str:
    """Today's schema with character_cards in its pre-0073 order."""
    block = schema.table_create_sql("character_cards")
    head, marker, _ = block.partition("    -- Last on purpose")
    assert marker, "the avatar_b64 comment moved; update this fixture"
    legacy = head.rstrip().rstrip(",") + "\n)"
    legacy = legacy.replace("    avatar_mime TEXT", "    avatar_b64 TEXT DEFAULT NULL,\n    avatar_mime TEXT", 1)
    return schema.CREATE_TABLES_SQL.replace(block, legacy, 1)


def _columns(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(character_cards)")]


def _legacy_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_legacy_schema())
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO character_cards (id, name, avatar_b64, avatar_mime, created_at, updated_at, extensions) "
        "VALUES ('c1', 'Mira', 'QUJD', 'image/png', '2026-01-01', '2026-01-02', '{\"k\": 1}')"
    )
    conn.execute("INSERT INTO character_expressions (character_card_id, label, data_b64) VALUES ('c1', 'joy', 'eA==')")
    conn.commit()
    return conn


def test_fresh_schema_stores_the_avatar_last():
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema.CREATE_TABLES_SQL)
    assert _columns(conn)[-1] == "avatar_b64"


def test_rebuild_moves_the_avatar_last_and_keeps_every_value():
    conn = _legacy_db()
    assert _columns(conn)[-1] != "avatar_b64"
    before = dict(zip(_columns(conn), conn.execute("SELECT * FROM character_cards").fetchone(), strict=True))

    _migration.migrate(conn)

    assert _columns(conn)[-1] == "avatar_b64"
    after = dict(zip(_columns(conn), conn.execute("SELECT * FROM character_cards").fetchone(), strict=True))
    assert after == before
    # Dropping the parent with FKs on would have cascaded into the expressions.
    assert conn.execute("SELECT label FROM character_expressions").fetchall() == [("joy",)]
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rerun_is_a_no_op():
    conn = _legacy_db()
    _migration.migrate(conn)
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'character_cards'").fetchone()
    _migration.migrate(conn)
    assert conn.execute("SELECT sql FROM sqlite_master WHERE name = 'character_cards'").fetchone() == ddl


def test_missing_table_is_a_no_op():
    _migration.migrate(sqlite3.connect(":memory:"))


def test_the_upgrade_chain_ends_with_the_avatar_last(tmp_path: Path):
    db = tmp_path / "upgraded.db"
    conn = sqlite3.connect(db)
    conn.executescript(_BASELINE.read_text())
    conn.close()
    run_pending(db)
    conn = sqlite3.connect(db)
    try:
        assert _columns(conn)[-1] == "avatar_b64"
    finally:
        conn.close()
