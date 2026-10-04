from __future__ import annotations


async def test_list_mood_fragments_returns_seeded_data(client, db):
    resp = await client.get_json("/api/fragments")
    mood_fragments = resp
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
    resp = await client.post_json("/api/fragments", json=payload)
    assert resp["id"] == "test-frag"
    assert resp["cooldown_turns"] == 4

    row = await db.one("SELECT * FROM mood_fragments WHERE id = 'test-frag'")
    assert row is not None
    assert row["label"] == "Test"
    assert row["prompt_text"] == "Write dramatically."
    assert row["cooldown_turns"] == 4


async def test_create_duplicate_mood_fragment_returns_400(client, db):
    payload = {"id": "dupe", "label": "Dupe", "description": "x", "prompt_text": "x"}
    await client.post("/api/fragments", json=payload)
    await client.post_checked("/api/fragments", json=payload, expected_status=400)


async def test_mood_fragment_cooldown_is_bounded(client, db):
    payload = {"id": "bounded", "label": "Bounded", "description": "x", "prompt_text": "x"}
    for value in (-1, 51):
        await client.post_checked("/api/fragments", json={**payload, "cooldown_turns": value}, expected_status=422)


async def test_update_mood_fragment_persists_to_db(client, db):
    payload = {"id": "upd-frag", "label": "Original", "description": "desc", "prompt_text": "original text"}
    await client.post("/api/fragments", json=payload)

    resp = await client.put_json(
        "/api/fragments/upd-frag", json={"label": "Updated", "prompt_text": "new text", "cooldown_turns": 6}
    )
    assert resp["label"] == "Updated"
    assert resp["cooldown_turns"] == 6

    row = await db.one("SELECT label, prompt_text, cooldown_turns FROM mood_fragments WHERE id = 'upd-frag'")
    assert row["label"] == "Updated"
    assert row["prompt_text"] == "new text"
    assert row["cooldown_turns"] == 6


async def test_delete_mood_fragment_removes_from_db(client, db):
    payload = {"id": "del-frag", "label": "ToDelete", "description": "desc", "prompt_text": "text"}
    await client.post("/api/fragments", json=payload)

    await client.delete_checked("/api/fragments/del-frag")

    row = await db.one("SELECT id FROM mood_fragments WHERE id = 'del-frag'")
    assert row is None


async def test_delete_nonexistent_mood_fragment_returns_404(client, db):
    await client.delete_checked("/api/fragments/does-not-exist", expected_status=404)


async def test_update_nonexistent_mood_fragment_returns_404(client, db):
    await client.put_checked("/api/fragments/ghost", json={"label": "Ghost"}, expected_status=404)
