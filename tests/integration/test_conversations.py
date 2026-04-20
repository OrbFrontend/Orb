from __future__ import annotations


async def test_create_conversation_persists_to_db(client, db):
    resp = await client.post("/api/conversations", json={"title": "My Chat"})
    assert resp.status_code == 200
    cid = resp.json()["id"]
    assert cid

    async with db.execute(
        "SELECT title FROM conversations WHERE id = ?", (cid,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["title"] == "My Chat"


async def test_create_conversation_also_seeds_director_state(client, db):
    resp = await client.post("/api/conversations", json={})
    cid = resp.json()["id"]

    async with db.execute(
        "SELECT active_moods FROM director_state WHERE conversation_id = ?", (cid,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["active_moods"] == "[]"


async def test_list_conversations_includes_created(client, db):
    await client.post("/api/conversations", json={"title": "Listed"})
    resp = await client.get("/api/conversations")
    assert resp.status_code == 200
    titles = [c["title"] for c in resp.json()]
    assert "Listed" in titles


async def test_delete_conversation_removes_from_db(client, db):
    resp = await client.post("/api/conversations", json={"title": "ToDelete"})
    cid = resp.json()["id"]

    del_resp = await client.delete(f"/api/conversations/{cid}")
    assert del_resp.status_code == 200

    async with db.execute("SELECT id FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row is None


async def test_delete_nonexistent_conversation_returns_404(client, db):
    resp = await client.delete("/api/conversations/no-such-conv")
    assert resp.status_code == 404


async def test_get_messages_on_new_conversation_returns_empty(client, db):
    resp = await client.post("/api/conversations", json={})
    cid = resp.json()["id"]

    msgs = await client.get(f"/api/conversations/{cid}/messages")
    assert msgs.status_code == 200
    assert msgs.json() == []


async def test_conversation_with_first_mes_creates_assistant_message(client, db):
    resp = await client.post(
        "/api/conversations",
        json={"title": "Greeted", "first_mes": "Hello, traveller."},
    )
    assert resp.status_code == 200
    cid = resp.json()["id"]

    msgs = await client.get(f"/api/conversations/{cid}/messages")
    assert msgs.status_code == 200
    messages = msgs.json()
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "Hello, traveller."

    # Verify message is in DB
    async with db.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ?", (cid,)
    ) as cur:
        row = await cur.fetchone()
    assert row["role"] == "assistant"
    assert row["content"] == "Hello, traveller."


async def test_touch_conversation_updates_timestamp(client, db):
    resp = await client.post("/api/conversations", json={})
    cid = resp.json()["id"]

    async with db.execute(
        "SELECT updated_at FROM conversations WHERE id = ?", (cid,)
    ) as cur:
        before = (await cur.fetchone())["updated_at"]

    import asyncio

    await asyncio.sleep(0.01)  # ensure clock advances

    touch = await client.post(f"/api/conversations/{cid}/touch")
    assert touch.status_code == 200

    async with db.execute(
        "SELECT updated_at FROM conversations WHERE id = ?", (cid,)
    ) as cur:
        after = (await cur.fetchone())["updated_at"]

    assert after >= before


async def test_conversation_with_character_card(client, db):
    card_resp = await client.post(
        "/api/characters",
        json={
            "name": "Aria",
            "description": "An elf ranger.",
            "first_mes": "Greetings from the forest.",
            "scenario": "Deep woods",
        },
    )
    assert card_resp.status_code == 200
    card_id = card_resp.json()["id"]

    conv_resp = await client.post(
        "/api/conversations", json={"character_card_id": card_id}
    )
    assert conv_resp.status_code == 200
    conv = conv_resp.json()
    cid = conv["id"]
    assert conv["title"] == "Aria"
    assert conv["character_name"] == "Aria"

    # first_mes should be auto-added as the first assistant message
    msgs = await client.get(f"/api/conversations/{cid}/messages")
    assert msgs.json()[0]["content"] == "Greetings from the forest."

    # Verify link in DB
    async with db.execute(
        "SELECT character_card_id FROM conversations WHERE id = ?", (cid,)
    ) as cur:
        row = await cur.fetchone()
    assert row["character_card_id"] == card_id
