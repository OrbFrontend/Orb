"""The shipped default character reaches both kinds of install, exactly once.

A brand new database seeds it in ``bootstrap``; an existing one gets it from
migration 0061. Neither path runs twice, which is what makes deleting the
character stick -- that is the behaviour these tests pin, along with the
agreement between the shipped PNG and the row transcribed from it in ``seeds``.

The per-test fixture database deliberately strips this character (see
``conftest._fresh_db_template``), so every test here builds its own.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import backend.database.connection as db_connection
from backend.database import init_db
from backend.database.migrations import run_pending, stamp_all
from backend.database.seeds import (
    DEFAULT_CHARACTER,
    DEFAULT_CHARACTER_ID,
    DEFAULT_CHARACTER_PNG,
    DEFAULT_CHARACTER_WORLD,
    DEFAULT_CHARACTER_WORLD_ID,
)
from backend.features.cards import card_to_dict, parse, read_orb_id


def _card(path: Path) -> dict | None:
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM character_cards WHERE id = ?", (DEFAULT_CHARACTER_ID,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _count(path: Path, sql: str, *params) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


async def _fresh_install(path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    await init_db()
    stamp_all(path)


# --- the shipped PNG is the source the seed was transcribed from -------------


def test_seed_matches_the_shipped_card_file():
    """``seeds`` carries the card's fields as literals; the PNG carries them for
    real. Editing one without the other would ship a library entry that does not
    match the card the avatar column hands back on export."""
    assert read_orb_id(str(DEFAULT_CHARACTER_PNG)) == DEFAULT_CHARACTER_ID
    parsed = card_to_dict(parse(str(DEFAULT_CHARACTER_PNG)))

    book = parsed.pop("character_book")
    assert book["name"] == DEFAULT_CHARACTER_WORLD["name"]
    assert bool(book["extensions"]["orb"]["dynamic_enabled"]) == bool(DEFAULT_CHARACTER_WORLD["dynamic_enabled"])

    seeded = {k: v for k, v in DEFAULT_CHARACTER.items() if k not in ("id", "world_id")}
    assert parsed == seeded


# --- fresh installs ---------------------------------------------------------


async def test_a_fresh_install_ships_the_character_linked_to_its_world(tmp_path: Path, monkeypatch):
    path = tmp_path / "fresh.db"
    await _fresh_install(path, monkeypatch)

    card = _card(path)
    assert card is not None
    assert card["name"] == "Assistant"
    assert card["source_format"] == "tavern_v3"
    assert card["world_id"] == DEFAULT_CHARACTER_WORLD_ID
    assert json.loads(card["tags"]) == ["Non-human"]
    # The avatar column holds the whole card file, as the import route writes it.
    assert base64.b64decode(card["avatar_b64"]) == DEFAULT_CHARACTER_PNG.read_bytes()
    assert card["avatar_mime"] == "image/png"

    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        world = dict(conn.execute("SELECT * FROM worlds WHERE id = ?", (DEFAULT_CHARACTER_WORLD_ID,)).fetchone())
    finally:
        conn.close()
    assert world["name"] == DEFAULT_CHARACTER_WORLD["name"]
    assert world["dynamic_enabled"] == 1
    # An empty Dynamic World: the Agent writes its lore during play.
    assert _count(path, "SELECT COUNT(*) FROM lorebook_entries WHERE world_id = ?", DEFAULT_CHARACTER_WORLD_ID) == 0


async def test_the_card_carries_its_embedded_mood_fragment(tmp_path: Path, monkeypatch):
    path = tmp_path / "fresh.db"
    await _fresh_install(path, monkeypatch)

    card = _card(path)
    assert card is not None
    fragments = json.loads(card["extensions"])["orb"]["fragments"]
    assert [f["id"] for f in fragments["mood"]] == ["reveal_secret"]


async def test_deleting_it_keeps_it_deleted_across_restarts(tmp_path: Path, monkeypatch):
    """The seed is gated on first boot, not on an empty library, so a user who
    removes the character does not find it back after the next start."""
    path = tmp_path / "fresh.db"
    await _fresh_install(path, monkeypatch)

    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM character_cards WHERE id = ?", (DEFAULT_CHARACTER_ID,))
        conn.commit()
    finally:
        conn.close()

    await init_db()  # a second boot
    run_pending(path)  # and the chain, which is already stamped
    assert _card(path) is None


# --- existing installs ------------------------------------------------------


async def test_migration_0061_gives_an_existing_install_the_same_character(tmp_path: Path, monkeypatch):
    """Upgrade path: a database built before the seed existed picks it up once."""
    path = tmp_path / "existing.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    await init_db()
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM character_cards WHERE id = ?", (DEFAULT_CHARACTER_ID,))
        conn.execute("DELETE FROM worlds WHERE id = ?", (DEFAULT_CHARACTER_WORLD_ID,))
        conn.commit()
    finally:
        conn.close()

    run_pending(path)

    card = _card(path)
    assert card is not None
    assert card["world_id"] == DEFAULT_CHARACTER_WORLD_ID
    assert base64.b64decode(card["avatar_b64"]) == DEFAULT_CHARACTER_PNG.read_bytes()


async def test_migration_0061_leaves_an_already_imported_copy_alone(tmp_path: Path, monkeypatch):
    """The card id is the PNG's own ``orb_id``, so a user who imported the
    shipped file already owns this row -- edits included."""
    path = tmp_path / "existing.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    await init_db()
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE character_cards SET name = 'Mine' WHERE id = ?", (DEFAULT_CHARACTER_ID,))
        conn.commit()
    finally:
        conn.close()

    run_pending(path)

    assert _count(path, "SELECT COUNT(*) FROM character_cards WHERE id = ?", DEFAULT_CHARACTER_ID) == 1
    card = _card(path)
    assert card is not None and card["name"] == "Mine"


async def test_migration_0061_links_a_world_the_user_already_named(tmp_path: Path, monkeypatch):
    """A same-named world is reused rather than duplicated, the way the import
    route materializes an embedded ``character_book``."""
    path = tmp_path / "existing.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    await init_db()
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM character_cards WHERE id = ?", (DEFAULT_CHARACTER_ID,))
        conn.execute("UPDATE worlds SET id = 'mine' WHERE id = ?", (DEFAULT_CHARACTER_WORLD_ID,))
        conn.commit()
    finally:
        conn.close()

    run_pending(path)

    card = _card(path)
    assert card is not None and card["world_id"] == "mine"
    assert _count(path, "SELECT COUNT(*) FROM worlds WHERE name = ?", DEFAULT_CHARACTER_WORLD["name"]) == 1


async def test_re_running_the_migration_never_adds_a_second_copy(tmp_path: Path, monkeypatch):
    """The chain records 0061 as applied, so this is belt-and-braces -- but a
    guard that only works because it is never re-entered is not a guard."""
    path = tmp_path / "existing.db"
    monkeypatch.setattr(db_connection, "DB_PATH", str(path))
    await init_db()
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM character_cards WHERE id = ?", (DEFAULT_CHARACTER_ID,))
        conn.execute("DELETE FROM worlds WHERE id = ?", (DEFAULT_CHARACTER_WORLD_ID,))
        conn.commit()
    finally:
        conn.close()

    for _ in range(3):
        run_pending(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("DELETE FROM schema_migrations WHERE id = '0061_default_character'")
            conn.commit()
        finally:
            conn.close()

    assert _count(path, "SELECT COUNT(*) FROM character_cards WHERE id = ?", DEFAULT_CHARACTER_ID) == 1
    assert _count(path, "SELECT COUNT(*) FROM worlds WHERE name = ?", DEFAULT_CHARACTER_WORLD["name"]) == 1
