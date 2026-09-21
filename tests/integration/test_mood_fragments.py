from __future__ import annotations


async def test_list_mood_fragments_returns_seeded_data(client, db):
    resp = await client.get("/api/fragments")
    assert resp.status_code == 200
    mood_fragments = resp.json()
    ids = {f["id"] for f in mood_fragments}
    # These are seeded by init_db
    assert "talkative" in ids


async def test_create_mood_fragment_persists_to_db(client, db):
    payload = {
        "id": "test-frag",
        "label": "Test",
        "description": "A test mood fragment",
        "prompt_text": "Write dramatically.",
        "negative_prompt": "Do not write dramatically.",
        "cooldown_turns": 4,
    }
    resp = await client.post("/api/fragments", json=payload)
    assert resp.status_code == 200
    assert resp.json()["id"] == "test-frag"
    assert resp.json()["cooldown_turns"] == 4

    async with db.execute("SELECT * FROM mood_fragments WHERE id = 'test-frag'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["label"] == "Test"
    assert row["prompt_text"] == "Write dramatically."
    assert row["cooldown_turns"] == 4


async def test_create_duplicate_mood_fragment_returns_400(client, db):
    payload = {
        "id": "dupe",
        "label": "Dupe",
        "description": "x",
        "prompt_text": "x",
    }
    await client.post("/api/fragments", json=payload)
    resp = await client.post("/api/fragments", json=payload)
    assert resp.status_code == 400


async def test_mood_fragment_cooldown_is_bounded(client, db):
    payload = {"id": "bounded", "label": "Bounded", "description": "x", "prompt_text": "x"}
    for value in (-1, 51):
        response = await client.post("/api/fragments", json={**payload, "cooldown_turns": value})
        assert response.status_code == 422


async def test_update_mood_fragment_persists_to_db(client, db):
    payload = {
        "id": "upd-frag",
        "label": "Original",
        "description": "desc",
        "prompt_text": "original text",
    }
    await client.post("/api/fragments", json=payload)

    resp = await client.put(
        "/api/fragments/upd-frag",
        json={"label": "Updated", "prompt_text": "new text", "cooldown_turns": 6},
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "Updated"
    assert resp.json()["cooldown_turns"] == 6

    async with db.execute("SELECT label, prompt_text, cooldown_turns FROM mood_fragments WHERE id = 'upd-frag'") as cur:
        row = await cur.fetchone()
    assert row["label"] == "Updated"
    assert row["prompt_text"] == "new text"
    assert row["cooldown_turns"] == 6


async def test_delete_mood_fragment_removes_from_db(client, db):
    payload = {
        "id": "del-frag",
        "label": "ToDelete",
        "description": "desc",
        "prompt_text": "text",
    }
    await client.post("/api/fragments", json=payload)

    resp = await client.delete("/api/fragments/del-frag")
    assert resp.status_code == 200

    async with db.execute("SELECT id FROM mood_fragments WHERE id = 'del-frag'") as cur:
        row = await cur.fetchone()
    assert row is None


async def test_delete_nonexistent_mood_fragment_returns_404(client, db):
    resp = await client.delete("/api/fragments/does-not-exist")
    assert resp.status_code == 404


async def test_update_nonexistent_mood_fragment_returns_404(client, db):
    resp = await client.put("/api/fragments/ghost", json={"label": "Ghost"})
    assert resp.status_code == 404
