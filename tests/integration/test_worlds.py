async def test_lorebook_export_round_trip(client, db):
    wid = (await client.post_json("/api/worlds", json={"name": "Test Realm"}))["id"]

    await client.post(
        f"/api/worlds/{wid}/entries",
        json={
            "name": "Dragons",
            "content": "Dragons breathe fire.",
            "keywords": ["dragon", "wyrm"],
            "case_insensitive": True,
            "constant": False,
            "priority": 50,
            "enabled": True,
        },
    )
    await client.post(
        f"/api/worlds/{wid}/entries",
        json={
            "name": "Prologue",
            "content": "Always present.",
            "keywords": [],
            "case_insensitive": False,
            "constant": True,
            "priority": 100,
            "enabled": True,
        },
    )

    resp = await client.get_checked(f"/api/worlds/{wid}/export")
    assert resp.headers["content-type"].startswith("application/json")
    assert 'filename="Test Realm.json"' in resp.headers["content-disposition"]

    book = resp.json()
    assert book["name"] == "Test Realm"
    by_name = {e["name"]: e for e in book["entries"]}
    assert by_name["Dragons"]["keys"] == ["dragon", "wyrm"]
    assert by_name["Dragons"]["case_sensitive"] is False
    assert by_name["Dragons"]["priority"] == 50
    assert by_name["Dragons"]["constant"] is False
    assert by_name["Prologue"]["constant"] is True
    assert by_name["Prologue"]["case_sensitive"] is True

    # The export must be accepted verbatim by the import endpoint, losslessly
    world2 = await client.post_json("/api/worlds", json={"name": "Copy"})
    assert (await client.post_json(f"/api/worlds/{world2['id']}/import", json={"entries": book["entries"]}))["imported"] == 2

    copied = {e["name"]: e for e in await client.get_json(f"/api/worlds/{world2['id']}/entries")}
    assert copied["Dragons"]["keywords"] == ["dragon", "wyrm"]
    assert bool(copied["Dragons"]["case_insensitive"]) is True
    assert copied["Dragons"]["priority"] == 50
    assert bool(copied["Prologue"]["constant"]) is True
    assert bool(copied["Prologue"]["case_insensitive"]) is False


async def test_import_world_info_file_maps_at_depth(client, db):
    """A standalone World Info export (entries as an object, `position: 4` = @ Depth).

    This is the shape community "rules module" lorebooks ship in -- always-on
    entries injected after the latest message so their {{roll}} macros re-roll.
    """
    world = await client.post_json("/api/worlds", json={"name": "V20"})
    payload = {
        "entries": {
            "0": {
                "uid": 0,
                "key": [],
                "comment": "Rules",
                "content": "Pool: {{roll::1d10}}",
                "constant": True,
                "position": 4,
                "order": 100,
            },
            "1": {
                "uid": 1,
                "key": [],
                "comment": "Sheet",
                "content": "{{// fill me }}Strength: 1",
                "constant": True,
                "position": 1,
                "disable": True,
            },
        }
    }
    assert (await client.post_json(f"/api/worlds/{world['id']}/import", json=payload))["imported"] == 2

    entries = {e["name"]: e for e in await client.get_json(f"/api/worlds/{world['id']}/entries")}
    assert bool(entries["Rules"]["at_depth"]) is True
    assert bool(entries["Rules"]["constant"]) is True
    assert bool(entries["Sheet"]["at_depth"]) is False  # position 1 = after char defs
    assert bool(entries["Sheet"]["enabled"]) is False  # `disable: true`
    # Comments are stripped at render time, not on the way in.
    assert "{{//" in entries["Sheet"]["content"]

    # Orb's own export carries the flag back through an import (lossless).
    book = await client.get_json(f"/api/worlds/{world['id']}/export")
    world2 = await client.post_json("/api/worlds", json={"name": "Copy"})
    await client.post(f"/api/worlds/{world2['id']}/import", json={"entries": book["entries"]})
    copied = {e["name"]: e for e in await client.get_json(f"/api/worlds/{world2['id']}/entries")}
    assert bool(copied["Rules"]["at_depth"]) is True
    assert bool(copied["Sheet"]["at_depth"]) is False


async def test_character_book_extensions_round_trip(client, db):
    """The card-embedded `character_book` shape: placement + case live in `extensions`.

    World Info readers take `extensions.position` / `extensions.case_sensitive` and title the entry from `comment`, so the
    export has to fill those in or a round-trip through another frontend loses all three.
    """
    world = await client.post_json("/api/worlds", json={"name": "Book"})
    payload = {
        "entries": [
            {
                "keys": ["dragon"],
                "content": "Dragons breathe fire.",
                "comment": "Dragons",
                "enabled": True,
                "position": "after_char",
                "extensions": {"position": 4, "depth": 2, "case_sensitive": True},
            }
        ]
    }
    assert (await client.post_json(f"/api/worlds/{world['id']}/import", json=payload))["imported"] == 1

    entry = (await client.get_json(f"/api/worlds/{world['id']}/entries"))[0]
    assert entry["name"] == "Dragons"
    assert bool(entry["at_depth"]) is True
    assert bool(entry["case_insensitive"]) is False

    book = await client.get_json(f"/api/worlds/{world['id']}/export")
    exported = book["entries"][0]
    assert exported["comment"] == "Dragons"  # readers take the title from here
    assert exported["extensions"]["position"] == 4
    assert exported["extensions"]["case_sensitive"] is True

    world2 = await client.post_json("/api/worlds", json={"name": "Copy"})
    await client.post(f"/api/worlds/{world2['id']}/import", json={"entries": book["entries"]})
    copied = (await client.get_json(f"/api/worlds/{world2['id']}/entries"))[0]
    assert bool(copied["at_depth"]) is True
    assert bool(copied["case_insensitive"]) is False


async def test_lorebook_export_missing_world_404(client, db):
    await client.get_checked("/api/worlds/no-such-world/export", expected_status=404)


async def test_reading_scene_worlds_preserves_other_scenes_and_recency(client):
    linked = await client.post_json("/api/worlds", json={"name": "Elsinore"})
    global_world = await client.post_json("/api/worlds", json={"name": "Global", "is_global": True})
    card = await client.post_json("/api/characters", json={"name": "Hamlet", "world_id": linked["id"]})
    a = await client.post_json("/api/conversations", json={"character_card_id": card["id"]})
    b = await client.post_json("/api/conversations", json={})
    before = await client.get_json("/api/worlds")
    assert set((await client.get_json(f"/api/conversations/{a['id']}/worlds"))["world_ids"]) == {
        linked["id"],
        global_world["id"],
    }
    assert (await client.get_json(f"/api/conversations/{b['id']}/worlds"))["world_ids"] == [global_world["id"]]
    assert await client.get_json("/api/worlds") == before


async def test_choices_override_defaults_copy_on_fork_and_survive_partial_preset(client, db):
    from backend.database import fork_conversation, get_conversation

    global_world = await client.create("/api/worlds", json={"name": "Global", "is_global": True})
    floating = await client.create("/api/worlds", json={"name": "Floating"})
    cid = await client.create("/api/conversations", json={})
    await client.put(f"/api/conversations/{cid}/worlds/{global_world}", json={"enabled": False})
    await client.put(f"/api/conversations/{cid}/worlds/{floating}", json={"enabled": True})
    assert (await client.get_json(f"/api/conversations/{cid}/worlds"))["world_ids"] == [floating]
    # Matching the default removes the override, so future default changes apply.
    await client.put(f"/api/conversations/{cid}/worlds/{floating}", json={"enabled": False})
    assert not await db.execute_fetchall(
        "SELECT 1 FROM conversation_worlds WHERE conversation_id = ? AND world_id = ?", (cid, floating)
    )
    source = await get_conversation(cid)
    assert source is not None
    fork = await fork_conversation(source, "Fork")
    assert (await client.get_json(f"/api/conversations/{fork}/worlds"))["world_ids"] == []
    name = (await client.post_json("/api/presets/export", json={"domains": ["chats"]}))["name"]
    await client.delete(f"/api/worlds/{global_world}")
    await client.post_checked(f"/api/presets/{name}/apply")
    assert not await db.execute_fetchall("SELECT 1 FROM conversation_worlds WHERE world_id = ?", (global_world,))
