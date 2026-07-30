from __future__ import annotations


async def test_lorebook_export_round_trip(client, db):
    world = (await client.post("/api/worlds", json={"name": "Test Realm"})).json()
    wid = world["id"]

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

    resp = await client.get(f"/api/worlds/{wid}/export")
    assert resp.status_code == 200
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
    world2 = (await client.post("/api/worlds", json={"name": "Copy"})).json()
    imp = await client.post(f"/api/worlds/{world2['id']}/import", json={"entries": book["entries"]})
    assert imp.status_code == 200
    assert imp.json()["imported"] == 2

    copied = {e["name"]: e for e in (await client.get(f"/api/worlds/{world2['id']}/entries")).json()}
    assert copied["Dragons"]["keywords"] == ["dragon", "wyrm"]
    assert bool(copied["Dragons"]["case_insensitive"]) is True
    assert copied["Dragons"]["priority"] == 50
    assert bool(copied["Prologue"]["constant"]) is True
    assert bool(copied["Prologue"]["case_insensitive"]) is False


async def test_import_sillytavern_world_info_maps_at_depth(client, db):
    """A SillyTavern World Info export (entries as an object, `position: 4` = @ Depth).

    This is the shape community "rules module" lorebooks ship in — always-on
    entries injected after the latest message so their {{roll}} macros re-roll.
    """
    world = (await client.post("/api/worlds", json={"name": "V20"})).json()
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
    imp = await client.post(f"/api/worlds/{world['id']}/import", json=payload)
    assert imp.status_code == 200
    assert imp.json()["imported"] == 2

    entries = {e["name"]: e for e in (await client.get(f"/api/worlds/{world['id']}/entries")).json()}
    assert bool(entries["Rules"]["at_depth"]) is True
    assert bool(entries["Rules"]["constant"]) is True
    assert bool(entries["Sheet"]["at_depth"]) is False  # position 1 = after char defs
    assert bool(entries["Sheet"]["enabled"]) is False  # `disable: true`
    # Comments are stripped at render time, not on the way in.
    assert "{{//" in entries["Sheet"]["content"]

    # Orb's own export carries the flag back through an import (lossless).
    book = (await client.get(f"/api/worlds/{world['id']}/export")).json()
    world2 = (await client.post("/api/worlds", json={"name": "Copy"})).json()
    await client.post(f"/api/worlds/{world2['id']}/import", json={"entries": book["entries"]})
    copied = {e["name"]: e for e in (await client.get(f"/api/worlds/{world2['id']}/entries")).json()}
    assert bool(copied["Rules"]["at_depth"]) is True
    assert bool(copied["Sheet"]["at_depth"]) is False


async def test_lorebook_export_missing_world_404(client, db):
    resp = await client.get("/api/worlds/no-such-world/export")
    assert resp.status_code == 404
