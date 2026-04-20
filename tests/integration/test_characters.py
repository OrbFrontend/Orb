from __future__ import annotations

import json


async def test_create_character_persists_to_db(client, db):
    payload = {
        "name": "Lira",
        "description": "A wandering bard.",
        "personality": "Cheerful",
        "scenario": "A tavern",
        "tags": ["fantasy", "bard"],
    }
    resp = await client.post("/api/characters", json=payload)
    assert resp.status_code == 200
    card_id = resp.json()["id"]

    async with db.execute(
        "SELECT name, description, tags FROM character_cards WHERE id = ?", (card_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["name"] == "Lira"
    assert row["description"] == "A wandering bard."
    assert json.loads(row["tags"]) == ["fantasy", "bard"]


async def test_list_characters_includes_created(client, db):
    await client.post("/api/characters", json={"name": "Rook"})
    resp = await client.get("/api/characters")
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()]
    assert "Rook" in names


async def test_get_character_by_id(client, db):
    create_resp = await client.post(
        "/api/characters", json={"name": "Zara", "description": "A mage."}
    )
    card_id = create_resp.json()["id"]

    resp = await client.get(f"/api/characters/{card_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Zara"
    assert resp.json()["description"] == "A mage."


async def test_get_nonexistent_character_returns_404(client, db):
    resp = await client.get("/api/characters/no-such-id")
    assert resp.status_code == 404


async def test_update_character_persists_to_db(client, db):
    create_resp = await client.post(
        "/api/characters", json={"name": "Old Name", "scenario": "Old scenario"}
    )
    card_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/characters/{card_id}",
        json={"name": "New Name", "scenario": "New scenario"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"

    async with db.execute(
        "SELECT name, scenario FROM character_cards WHERE id = ?", (card_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["name"] == "New Name"
    assert row["scenario"] == "New scenario"


async def test_update_character_syncs_to_linked_conversations(client, db):
    create_resp = await client.post(
        "/api/characters",
        json={"name": "SyncChar", "scenario": "Original scenario"},
    )
    card_id = create_resp.json()["id"]

    conv_resp = await client.post(
        "/api/conversations", json={"character_card_id": card_id}
    )
    cid = conv_resp.json()["id"]

    await client.put(
        f"/api/characters/{card_id}", json={"scenario": "Updated scenario"}
    )

    async with db.execute(
        "SELECT character_scenario FROM conversations WHERE id = ?", (cid,)
    ) as cur:
        row = await cur.fetchone()
    assert row["character_scenario"] == "Updated scenario"


async def test_delete_character_removes_from_db(client, db):
    create_resp = await client.post("/api/characters", json={"name": "Doomed"})
    card_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/characters/{card_id}")
    assert resp.status_code == 200

    async with db.execute(
        "SELECT id FROM character_cards WHERE id = ?", (card_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is None


async def test_blank_character_name_returns_422(client, db):
    resp = await client.post("/api/characters", json={"name": "   "})
    assert resp.status_code == 422


async def test_update_blank_name_returns_422(client):
    create_resp = await client.post("/api/characters", json={"name": "Valid"})
    card_id = create_resp.json()["id"]

    resp = await client.put(f"/api/characters/{card_id}", json={"name": "  "})
    assert resp.status_code == 422


async def test_update_nonexistent_character_returns_404(client):
    resp = await client.put("/api/characters/no-such-id", json={"name": "Ghost"})
    assert resp.status_code == 404


async def test_create_character_with_explicit_id(client, db):
    resp = await client.post(
        "/api/characters", json={"id": "my-stable-id", "name": "Stable"}
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "my-stable-id"

    async with db.execute(
        "SELECT id FROM character_cards WHERE id = 'my-stable-id'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


async def test_alternate_greetings_stored_as_json(client, db):
    greetings = ["Hello there.", "Good day, stranger."]
    resp = await client.post(
        "/api/characters",
        json={"name": "Greeter", "alternate_greetings": greetings},
    )
    assert resp.status_code == 200
    card_id = resp.json()["id"]

    async with db.execute(
        "SELECT alternate_greetings FROM character_cards WHERE id = ?", (card_id,)
    ) as cur:
        row = await cur.fetchone()
    assert json.loads(row["alternate_greetings"]) == greetings


async def test_delete_character_keeps_conversations_by_default(client, db):
    card_resp = await client.post("/api/characters", json={"name": "Keeper"})
    card_id = card_resp.json()["id"]
    conv_resp = await client.post(
        "/api/conversations", json={"character_card_id": card_id}
    )
    cid = conv_resp.json()["id"]

    await client.delete(f"/api/characters/{card_id}")

    # Conversation must still exist; character_card_id is left as a dangling reference
    async with db.execute("SELECT id FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row is not None


async def test_delete_character_with_delete_conversations_flag(client, db):
    card_resp = await client.post("/api/characters", json={"name": "Purged"})
    card_id = card_resp.json()["id"]
    conv_resp = await client.post(
        "/api/conversations", json={"character_card_id": card_id}
    )
    cid = conv_resp.json()["id"]

    await client.delete(f"/api/characters/{card_id}?delete_conversations=true")

    async with db.execute("SELECT id FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row is None


async def test_update_character_updates_timestamp(client, db):
    create_resp = await client.post("/api/characters", json={"name": "Timestamped"})
    card_id = create_resp.json()["id"]
    original_ts = create_resp.json()["updated_at"]

    import asyncio

    await asyncio.sleep(0.01)

    await client.put(f"/api/characters/{card_id}", json={"description": "Changed."})

    async with db.execute(
        "SELECT updated_at FROM character_cards WHERE id = ?", (card_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["updated_at"] > original_ts


async def test_update_character_tags_persists_to_db(client, db):
    create_resp = await client.post(
        "/api/characters",
        json={"name": "Tagged", "tags": ["original"]},
    )
    card_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/characters/{card_id}", json={"tags": ["action", "drama"]}
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["action", "drama"]

    async with db.execute(
        "SELECT tags FROM character_cards WHERE id = ?", (card_id,)
    ) as cur:
        row = await cur.fetchone()
    assert json.loads(row["tags"]) == ["action", "drama"]


async def test_post_history_instructions_synced_on_update(client, db):
    card_resp = await client.post(
        "/api/characters",
        json={"name": "PostHist", "post_history_instructions": "Original instructions"},
    )
    card_id = card_resp.json()["id"]
    conv_resp = await client.post(
        "/api/conversations", json={"character_card_id": card_id}
    )
    cid = conv_resp.json()["id"]

    await client.put(
        f"/api/characters/{card_id}",
        json={"post_history_instructions": "New instructions"},
    )

    async with db.execute(
        "SELECT post_history_instructions FROM conversations WHERE id = ?", (cid,)
    ) as cur:
        row = await cur.fetchone()
    assert row["post_history_instructions"] == "New instructions"
