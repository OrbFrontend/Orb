from __future__ import annotations

import backend.database as dbmod


async def test_create_conversation_persists_to_db(client, db):
    resp = await client.post("/api/conversations", json={"title": "My Chat"})
    assert resp.status_code == 200
    cid = resp.json()["id"]
    assert cid

    async with db.execute("SELECT title FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["title"] == "My Chat"


async def test_create_conversation_also_seeds_director_state(client, db):
    resp = await client.post("/api/conversations", json={})
    cid = resp.json()["id"]

    async with db.execute("SELECT active_moods FROM director_state WHERE conversation_id = ?", (cid,)) as cur:
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
    async with db.execute("SELECT role, content FROM messages WHERE conversation_id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["role"] == "assistant"
    assert row["content"] == "Hello, traveller."


async def test_touch_conversation_updates_timestamp(client, db):
    resp = await client.post("/api/conversations", json={})
    cid = resp.json()["id"]

    async with db.execute("SELECT updated_at FROM conversations WHERE id = ?", (cid,)) as cur:
        before = (await cur.fetchone())["updated_at"]

    import asyncio

    await asyncio.sleep(0.01)  # ensure clock advances

    touch = await client.post(f"/api/conversations/{cid}/touch")
    assert touch.status_code == 200

    async with db.execute("SELECT updated_at FROM conversations WHERE id = ?", (cid,)) as cur:
        after = (await cur.fetchone())["updated_at"]

    # Strict >: touch must move the timestamp forward, not merely leave it
    # unchanged. updated_at is an ISO string with microsecond resolution
    # (touch_conversation), so the 0.01s sleep guarantees a later value.
    assert after > before


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

    conv_resp = await client.post("/api/conversations", json={"character_card_id": card_id})
    assert conv_resp.status_code == 200
    conv = conv_resp.json()
    cid = conv["id"]
    assert conv["title"] == "Aria"
    assert conv["character_name"] == "Aria"

    # first_mes should be auto-added as the first assistant message
    msgs = await client.get(f"/api/conversations/{cid}/messages")
    assert msgs.json()[0]["content"] == "Greetings from the forest."

    # Verify link in DB
    async with db.execute("SELECT character_card_id FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["character_card_id"] == card_id


async def test_checkpoint_duplicates_active_path(client, db):
    cid = "conv-checkpoint-src"
    await dbmod.create_conversation(cid, "My Story", "Bot", "a scenario")
    u1, _ = await dbmod.add_message(
        cid,
        "user",
        "hello",
        0,
        parent_id=None,
        attachments=[{"mime_type": "image/png", "data_b64": "QUJD", "filename": "a.png", "size": 3}],
    )
    a1, _ = await dbmod.add_message(cid, "assistant", "hi there", 1, parent_id=u1)
    await dbmod.set_active_leaf(cid, a1)
    await dbmod.update_director_state(cid, ["tense"], keywords=["k"], progressive_fields={"hp": 5})
    await dbmod.add_conversation_log(
        cid, 0, "raw output", [], ["tense"], "inj block", 12, progressive_fields={"hp": 5}, message_id=a1, feedback={}
    )

    resp = await client.post(f"/api/conversations/{cid}/checkpoint", json={})
    assert resp.status_code == 200
    new = resp.json()
    new_cid = new["id"]
    assert new_cid != cid
    assert new["title"] == "My Story (checkpoint)"

    msgs = (await client.get(f"/api/conversations/{new_cid}/messages")).json()
    assert [(m["role"], m["content"], m["turn_index"]) for m in msgs] == [
        ("user", "hello", 0),
        ("assistant", "hi there", 1),
    ]
    # Fresh row ids — the copy is a distinct message tree, not a shared reference.
    assert msgs[1]["id"] != a1
    # User upload carried onto the copy.
    assert msgs[0]["user_attachments"][0]["data_b64"] == "QUJD"

    # Director state carried verbatim so continuation behaves identically.
    ds = await dbmod.get_director_state(new_cid)
    assert ds["active_moods"] == ["tense"]
    assert ds["progressive_fields"] == {"hp": 5}

    # Inspector log carried and re-pointed onto the copied assistant message.
    log = await dbmod.get_director_log_for_message(msgs[1]["id"])
    assert log is not None
    assert log["agent_raw_output"] == "raw output"

    # Source conversation is untouched.
    src = (await client.get(f"/api/conversations/{cid}/messages")).json()
    assert len(src) == 2


async def test_checkpoint_copies_only_active_branch(client, db):
    cid = "conv-checkpoint-branch"
    await dbmod.create_conversation(cid, "Branched", "Bot", "scenario")
    u1, _ = await dbmod.add_message(cid, "user", "prompt", 0, parent_id=None)
    a_active, _ = await dbmod.add_message(cid, "assistant", "active reply", 1, parent_id=u1)
    await dbmod.add_message(cid, "assistant", "swipe reply", 1, parent_id=u1)  # alternate branch
    await dbmod.set_active_leaf(cid, a_active)

    resp = await client.post(f"/api/conversations/{cid}/checkpoint", json={})
    new_cid = resp.json()["id"]

    msgs = (await client.get(f"/api/conversations/{new_cid}/messages")).json()
    assert [m["content"] for m in msgs] == ["prompt", "active reply"]
    # Only the active path is copied — the alternate swipe is not carried.
    async with db.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (new_cid,)) as cur:
        assert (await cur.fetchone())["n"] == 2


async def test_checkpoint_accepts_custom_title(client, db):
    cid = "conv-checkpoint-title"
    await dbmod.create_conversation(cid, "Orig", "Bot", "scenario")
    m, _ = await dbmod.add_message(cid, "assistant", "hi", 0, parent_id=None)
    await dbmod.set_active_leaf(cid, m)

    resp = await client.post(f"/api/conversations/{cid}/checkpoint", json={"title": "  Saved Point  "})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Saved Point"


async def test_checkpoint_missing_conversation_returns_404(client, db):
    resp = await client.post("/api/conversations/no-such-conv/checkpoint", json={})
    assert resp.status_code == 404
