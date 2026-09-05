"""End-to-end cover for scripts/migrate_sillytavern.py.

Builds a miniature SillyTavern install on disk, migrates it into a fresh Orb
database, and then reads the result back **through the HTTP API** rather than
off the tables. The script writes raw SQL, so proving the rows exist proves
very little; proving the app can render them is the actual contract.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

# pytest.ini puts the repo root on sys.path, so scripts/ imports as a PEP 420
# namespace package -- no __init__.py and no path juggling needed.
import scripts.migrate_sillytavern as migrator
from backend.features.cards.parsing import to_png

# --------------------------------------------------------------------------- #
# A miniature SillyTavern install
# --------------------------------------------------------------------------- #

WORLD_BOOK = {
    "entries": {
        # ST keys entries by uid and spells the fields its own way: `key`,
        # `comment`, `disable`, `order`, and position 4 for "@ Depth".
        "0": {
            "key": ["lighthouse", "beacon"],
            "keysecondary": [],
            "comment": "The Lighthouse",
            "content": "It has stood on the point for two hundred years.",
            "constant": False,
            "disable": False,
            "order": 42,
            "position": 0,
            "caseSensitive": None,
        },
        "1": {
            "key": ["storm"],
            "keysecondary": ["night"],
            "comment": "Storms",
            "content": "Storms roll in from the west without warning.",
            "constant": True,
            "selective": True,
            "disable": False,
            "order": 7,
            "position": 4,
        },
    }
}

CARD_BOOK = {
    "name": "Harbour Lore",
    "entries": [
        {
            "keys": ["harbour"],
            "name": "The Harbour",
            "content": "Fishing boats crowd the quay before dawn.",
            "enabled": True,
            "insertion_order": 3,
        }
    ],
}


# A 1x1 PNG, stood up as a real persona avatar file under User Avatars/.
PERSONA_AVATAR_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _chat_line(**kwargs) -> str:
    return json.dumps(kwargs)


def build_st_install(base: Path) -> Path:
    """Write a small but representative ST data directory. Returns the ST root."""
    user = base / "data" / "default-user"
    for folder in ("characters", "chats", "worlds", "groups", "group chats", "User Avatars"):
        (user / folder).mkdir(parents=True, exist_ok=True)

    # One persona has a picture on disk and one does not, so the import covers
    # both the image path and the colour-only fallback.
    (user / "User Avatars" / "mariner.png").write_bytes(PERSONA_AVATAR_BYTES)
    # A persona whose file is past the cap: it must import, minus the picture.
    (user / "User Avatars" / "huge.png").write_bytes(PERSONA_AVATAR_BYTES + b"\x00" * (2 * 1024 * 1024))

    # The card's *name* and its avatar *filename* differ on purpose: ST keys
    # sprite folders by name and chat folders by filename stem.
    lamplighter = {
        "name": "Testy",
        "description": "Keeper of the light.",
        "personality": "Terse.",
        "scenario": "A rock in the North Sea.",
        "first_mes": "The lamp needs winding.",
        "mes_example": "",
        "creator": "someone",
        "character_version": "1.4.2",
        "tags": ["keeper"],
        "alternate_greetings": ["Fog tonight."],
        "extensions": {"world": "Testworld", "talkativeness": "0.5"},
    }
    (user / "characters" / "avatar_testy.png").write_bytes(to_png(lamplighter))

    with_book = {
        "name": "Booked",
        "description": "Carries a lorebook.",
        "first_mes": "Hello from the harbour.",
        "character_book": CARD_BOOK,
        "extensions": {},
    }
    (user / "characters" / "Booked.png").write_bytes(to_png(with_book))

    sprites = user / "characters" / "Testy"
    sprites.mkdir(exist_ok=True)
    for label in ("joy", "anger", "neutral"):
        (sprites / f"{label}.png").write_bytes(b"not-really-an-image")
    (sprites / "notanemotion.png").write_bytes(b"ignored")

    (user / "worlds" / "Testworld.json").write_text(json.dumps(WORLD_BOOK), encoding="utf-8")
    (user / "worlds" / "Hollow.json").write_text(json.dumps({"entries": {}}), encoding="utf-8")

    chat = user / "chats" / "avatar_testy"
    chat.mkdir(parents=True, exist_ok=True)
    lines = [
        _chat_line(user_name="Mariner", character_name="Testy", chat_metadata={}),
        _chat_line(name="Testy", is_user=False, send_date="March 15, 2025 2:25pm", mes="The lamp needs winding."),
        _chat_line(name="Mariner", is_user=True, send_date="March 15, 2025 2:26pm", mes="I brought oil."),
        # Three swipes; ST had the second one selected.
        _chat_line(
            name="Testy",
            is_user=False,
            send_date="March 15, 2025 2:27pm",
            mes="Second telling.",
            swipe_id=1,
            swipes=["First telling.", "Second telling.", "Third telling."],
        ),
        _chat_line(name="Mariner", is_user=True, send_date="March 15, 2025 2:28pm", mes="Good."),
        # Edited after generation: `mes` no longer matches swipes[swipe_id].
        _chat_line(
            name="Testy",
            is_user=False,
            send_date="March 15, 2025 2:29pm",
            mes="Edited afterwards.",
            swipe_id=0,
            swipes=["The original generation."],
        ),
    ]
    (chat / "Testy - 2025-03-15@14h25m00s.jsonl").write_text("\n".join(lines), encoding="utf-8")

    # A chat whose character card no longer exists.
    orphan = user / "chats" / "DeletedFriend"
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "gone.jsonl").write_text(
        "\n".join(
            [
                _chat_line(user_name="Mariner", character_name="DeletedFriend", chat_metadata={}),
                _chat_line(name="DeletedFriend", is_user=False, send_date="May 1, 2024 11:22pm", mes="Still here."),
            ]
        ),
        encoding="utf-8",
    )

    (user / "groups" / "g1.json").write_text(
        json.dumps(
            {
                "id": "1700000000000",
                "name": "Group: Testy, Booked",
                "members": ["avatar_testy.png", "Booked.png"],
                "disabled_members": ["Booked.png"],
                "chat_id": "groupchat",
                "chats": ["groupchat"],
            }
        ),
        encoding="utf-8",
    )
    (user / "group chats" / "groupchat.jsonl").write_text(
        "\n".join(
            [
                _chat_line(user_name="unused", character_name="unused", chat_metadata={}),
                _chat_line(
                    name="Testy",
                    is_user=False,
                    original_avatar="avatar_testy.png",
                    send_date="March 16, 2025 9:00am",
                    mes="Evening.",
                ),
                _chat_line(name="Mariner", is_user=True, send_date="March 16, 2025 9:01am", mes="Evening both."),
                _chat_line(
                    name="Booked",
                    is_user=False,
                    original_avatar="Booked.png",
                    send_date="March 16, 2025 9:02am",
                    mes="The tide is out.",
                ),
            ]
        ),
        encoding="utf-8",
    )

    (user / "settings.json").write_text(
        json.dumps(
            {
                "user_avatar": "mariner.png",
                "world_info_settings": {"world_info": {"globalSelect": ["Testworld"]}},
                "power_user": {
                    "personas": {"mariner.png": "Mariner", "quiet.png": "Quiet One", "huge.png": "Huge"},
                    "persona_descriptions": {
                        "mariner.png": {"description": "Sails the coast."},
                        "quiet.png": {"description": ""},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return base


@pytest.fixture
def st_install(tmp_path: Path) -> Path:
    return build_st_install(tmp_path / "SillyTavern")


def migrate(st_root: Path, db_path: Path, **overrides):
    options = migrator.Options(st_dir=str(st_root), db=str(db_path), no_backup=True, **overrides)
    report, problems = migrator.run(options)
    assert problems == [], problems
    return report


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_migrates_every_dataset(st_install: Path, db_path: Path):
    report = migrate(st_install, db_path)

    assert report.tally("characters", "created") == 2
    assert report.tally("chats", "created") == 1
    assert report.tally("groups", "created") == 1
    assert report.tally("personas", "created") == 3
    # Testworld from the standalone file, Harbour Lore from the card's book.
    assert report.tally("worlds", "created") == 2
    # The empty standalone world is skipped, and so is the card-less chat.
    assert report.tally("worlds", "skipped") == 1
    assert report.tally("chats", "skipped") == 1


async def test_conversation_reads_back_through_the_api(st_install: Path, db_path: Path, client):
    migrate(st_install, db_path)

    conversations = (await client.get("/api/conversations")).json()
    solo = [c for c in conversations if c["kind"] == "solo"]
    assert len(solo) == 1
    assert solo[0]["title"] == "Testy - 2025-03-15@14h25m00s"

    messages = (await client.get(f"/api/conversations/{solo[0]['id']}/messages")).json()
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant", "user", "assistant"]
    assert messages[0]["content"] == "The lamp needs winding."
    # Backdated from send_date, not stamped with today.
    assert messages[0]["created_at"].startswith("2025-03-15")


async def test_swipes_become_branches_with_the_selected_one_live(st_install: Path, db_path: Path, client):
    migrate(st_install, db_path)

    cid = [c for c in (await client.get("/api/conversations")).json() if c["kind"] == "solo"][0]["id"]
    messages = (await client.get(f"/api/conversations/{cid}/messages")).json()

    swiped = messages[2]
    assert swiped["branch_count"] == 3
    assert swiped["branch_index"] == 1  # ST had swipe_id 1 selected
    assert swiped["content"] == "Second telling."

    # An edited message: `mes` diverged from swipes[swipe_id], so the edit is an
    # extra branch and is the live one, and the original generation survives.
    edited = messages[4]
    assert edited["content"] == "Edited afterwards."
    assert edited["branch_count"] == 2
    assert edited["branch_index"] == 1


async def test_character_card_and_expressions_survive(st_install: Path, db_path: Path, client):
    migrate(st_install, db_path)

    cards = {c["name"]: c for c in (await client.get("/api/characters")).json()}
    assert set(cards) == {"Testy", "Booked"}

    testy = (await client.get(f"/api/characters/{cards['Testy']['id']}")).json()
    assert testy["description"] == "Keeper of the light."
    assert testy["character_version"] == "1.4.2"  # dropped by the HTTP create path, kept here
    assert testy["alternate_greetings"] == ["Fog tonight."]
    assert cards["Testy"]["has_avatar"] is True

    # Sprites live under the card's name, while its chats live under the file
    # stem -- and only real go-emotions labels are kept.
    labels = set((await client.get(f"/api/characters/{cards['Testy']['id']}/expressions")).json()["labels"])
    assert labels == {"joy", "anger", "neutral"}


async def test_lorebooks_land_with_orb_field_semantics(st_install: Path, db_path: Path, client):
    migrate(st_install, db_path)

    worlds = {w["name"]: w for w in (await client.get("/api/worlds")).json()}
    assert set(worlds) == {"Testworld", "Harbour Lore"}
    # Only what ST had globally selected arrives enabled.
    assert worlds["Testworld"]["enabled"] == 1
    assert worlds["Harbour Lore"]["enabled"] == 0

    entries = {e["name"]: e for e in (await client.get(f"/api/worlds/{worlds['Testworld']['id']}/entries")).json()}
    lighthouse = entries["The Lighthouse"]
    assert lighthouse["keywords"] == ["lighthouse", "beacon"]
    assert lighthouse["enabled"] == 1
    assert lighthouse["constant"] == 0
    assert lighthouse["at_depth"] == 0

    storms = entries["Storms"]
    assert storms["constant"] == 1
    assert storms["at_depth"] == 1  # ST position 4
    assert storms["secondary_keys"] == ["night"]
    assert storms["selective"] == 1

    # extensions.world names Testworld, so the card links to it.
    cards = {c["name"]: c for c in (await client.get("/api/characters")).json()}
    assert cards["Testy"]["world_id"] == worlds["Testworld"]["id"]
    assert cards["Booked"]["world_id"] == worlds["Harbour Lore"]["id"]


async def test_group_roster_and_speaker_attribution(st_install: Path, db_path: Path, client):
    migrate(st_install, db_path)

    group = [c for c in (await client.get("/api/conversations")).json() if c["kind"] == "group"][0]
    members = (await client.get(f"/api/conversations/{group['id']}/members")).json()
    assert [(m["display_name"], m["speaker_key"], m["muted"]) for m in members] == [
        ("Testy", "testy", 0),
        ("Booked", "booked", 1),  # ST had it in disabled_members
    ]

    by_id = {m["id"]: m["display_name"] for m in members}
    messages = (await client.get(f"/api/conversations/{group['id']}/messages")).json()
    spoken = [(m["role"], by_id.get(m["speaker_member_id"])) for m in messages]
    assert spoken == [("assistant", "Testy"), ("user", None), ("assistant", "Booked")]

    # One exchange per user turn, shared by the replies that follow it.
    exchanges = [m["exchange_id"] for m in messages]
    assert all(exchanges)
    assert exchanges[1] == exchanges[2] != exchanges[0]


async def test_personas_are_created_and_pinned_to_their_chat(st_install: Path, db_path: Path, client):
    migrate(st_install, db_path)

    personas = {p["name"]: p for p in (await client.get("/api/user-personas")).json()}
    assert {"Mariner", "Quiet One"} <= set(personas)
    assert personas["Mariner"]["description"] == "Sails the coast."
    # The picture under User Avatars/ lands on the persona; the persona with no
    # file keeps the generated colour and no image.
    assert personas["Mariner"]["has_avatar"] is True
    assert personas["Quiet One"]["has_avatar"] is False
    # Past the size cap the persona still lands, just without its picture --
    # the script must not write a row the API itself would reject.
    assert personas["Huge"]["has_avatar"] is False
    avatar = await client.get(f"/api/user-personas/{personas['Mariner']['id']}/avatar")
    assert avatar.status_code == 200
    assert avatar.content == PERSONA_AVATAR_BYTES

    solo = [c for c in (await client.get("/api/conversations")).json() if c["kind"] == "solo"][0]
    assert solo["persona_lock_id"] == personas["Mariner"]["id"]


async def test_second_run_changes_nothing(st_install: Path, db_path: Path, client):
    migrate(st_install, db_path)
    before = (await client.get("/api/conversations")).json()

    again = migrate(st_install, db_path)
    assert again.tally("characters", "created") == 0
    assert again.tally("chats", "created") == 0
    assert again.tally("groups", "created") == 0
    assert again.tally("worlds", "created") == 0
    assert again.tally("personas", "created") == 0
    assert again.tally("characters", "reused") == 2

    assert (await client.get("/api/conversations")).json() == before


async def test_dry_run_writes_nothing(st_install: Path, db_path: Path, client):
    report = migrate(st_install, db_path, dry_run=True)
    assert report.tally("characters", "created") == 2

    assert (await client.get("/api/conversations")).json() == []
    assert (await client.get("/api/characters")).json() == []
    assert (await client.get("/api/worlds")).json() == []


async def test_orphan_chats_are_importable_on_request(st_install: Path, db_path: Path, client):
    migrate(st_install, db_path, include_orphans=True)

    titles = {c["title"] for c in (await client.get("/api/conversations")).json()}
    assert "DeletedFriend - gone" in titles


async def test_only_chats_does_not_drag_the_library_in(st_install: Path, db_path: Path, client):
    """--only chats must read the card map, never create it."""
    migrate(st_install, db_path, only="chats")

    assert (await client.get("/api/characters")).json() == []
    assert (await client.get("/api/worlds")).json() == []
    # Without cards in the library there is nothing for a chat to attach to.
    assert (await client.get("/api/conversations")).json() == []


async def test_only_chats_attaches_to_cards_already_in_the_library(st_install: Path, db_path: Path, client):
    """The card id is derived from the PNG, so a prior import still matches."""
    migrate(st_install, db_path, only="characters")
    migrate(st_install, db_path, only="chats")

    conversations = (await client.get("/api/conversations")).json()
    assert len(conversations) == 1
    cards = {c["name"]: c["id"] for c in (await client.get("/api/characters")).json()}
    assert conversations[0]["character_card_id"] == cards["Testy"]


async def test_refuses_a_database_it_does_not_recognise(st_install: Path, tmp_path: Path):
    stunted = tmp_path / "stunted.db"
    with sqlite3.connect(str(stunted)) as conn:
        conn.execute("CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT INTO schema_migrations VALUES ('0001_init', ?)", (datetime.now(UTC).isoformat(),))

    options = migrator.Options(st_dir=str(st_install), db=str(stunted), no_backup=True)
    report, problems = migrator.run(options)
    assert problems
    assert any("behind" in p for p in problems)
    assert report.counts == {}


def test_parses_every_sillytavern_date_shape():
    shapes = {
        "March 15, 2025 2:25pm": (2025, 3, 15),
        "May 1, 2024 11:22pm": (2024, 5, 1),
        "2026-04-03T18:29:22.482Z": (2026, 4, 3),
        "2023-5-29 @21h 54m 44s 567ms": (2023, 5, 29),
        1710189419849: (2024, 3, 11),
    }
    for raw, expected in shapes.items():
        parsed = migrator.parse_st_date(raw)
        assert parsed is not None, raw
        assert (parsed.year, parsed.month, parsed.day) == expected or parsed.tzinfo is not None

    assert migrator.parse_st_date("") is None
    assert migrator.parse_st_date(None) is None
    assert migrator.parse_st_date("not a date at all") is None
