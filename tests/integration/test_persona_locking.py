"""Persona locking: the mirror persona_lock_id columns on conversations and
character_cards, set/cleared through the PUT routes, plus the delete-cleanup
that clears dangling locks when a locked persona is removed.

The resolution priority itself (conversation > character > global) is pinned as
a pure-function unit test in tests/unit/test_resolve_persona_id.py; here we pin
that the write paths and the explicit lock-clearing on delete actually persist.
"""

from __future__ import annotations


async def _make_persona(client, name):
    resp = await client.post("/api/user-personas", json={"name": name})
    assert resp.status_code == 200
    return resp.json()["id"]


async def test_conversation_lock_set_and_clear(client, db):
    persona_id = await _make_persona(client, "Kai")
    cid = (await client.post("/api/conversations", json={"title": "Locked"})).json()["id"]

    # Lock the conversation to the persona.
    resp = await client.put(f"/api/conversations/{cid}", json={"persona_lock_id": persona_id})
    assert resp.status_code == 200
    assert resp.json()["persona_lock_id"] == persona_id
    async with db.execute("SELECT persona_lock_id FROM conversations WHERE id = ?", (cid,)) as cur:
        assert (await cur.fetchone())["persona_lock_id"] == persona_id

    # An explicit null clears it (route uses exclude_unset, so null is honored).
    resp = await client.put(f"/api/conversations/{cid}", json={"persona_lock_id": None})
    assert resp.status_code == 200
    assert resp.json()["persona_lock_id"] is None
    async with db.execute("SELECT persona_lock_id FROM conversations WHERE id = ?", (cid,)) as cur:
        assert (await cur.fetchone())["persona_lock_id"] is None


async def test_character_lock_set_and_clear(client, db):
    persona_id = await _make_persona(client, "Kai")
    card_id = (await client.post("/api/characters", json={"name": "Lira"})).json()["id"]

    resp = await client.put(f"/api/characters/{card_id}", json={"persona_lock_id": persona_id})
    assert resp.status_code == 200
    assert resp.json()["persona_lock_id"] == persona_id
    # The list projection also surfaces the lock (frontend reads it from there).
    listed = (await client.get("/api/characters")).json()
    assert any(c["id"] == card_id and c["persona_lock_id"] == persona_id for c in listed)

    resp = await client.put(f"/api/characters/{card_id}", json={"persona_lock_id": None})
    assert resp.status_code == 200
    assert resp.json()["persona_lock_id"] is None
    async with db.execute("SELECT persona_lock_id FROM character_cards WHERE id = ?", (card_id,)) as cur:
        assert (await cur.fetchone())["persona_lock_id"] is None


async def test_lock_to_missing_persona_rejected(client, db):
    """Migrated DBs have no FK on persona_lock_id; the route must reject
    locks pointing at personas that don't exist (null still clears fine)."""
    cid = (await client.post("/api/conversations", json={"title": "Locked"})).json()["id"]
    resp = await client.put(f"/api/conversations/{cid}", json={"persona_lock_id": 99999})
    assert resp.status_code == 400

    card_id = (await client.post("/api/characters", json={"name": "Lira"})).json()["id"]
    resp = await client.put(f"/api/characters/{card_id}", json={"persona_lock_id": 99999})
    assert resp.status_code == 400

    async with db.execute("SELECT persona_lock_id FROM conversations WHERE id = ?", (cid,)) as cur:
        assert (await cur.fetchone())["persona_lock_id"] is None
    async with db.execute("SELECT persona_lock_id FROM character_cards WHERE id = ?", (card_id,)) as cur:
        assert (await cur.fetchone())["persona_lock_id"] is None


async def test_deleting_persona_clears_dangling_locks(client, db):
    persona_id = await _make_persona(client, "Kai")
    cid = (await client.post("/api/conversations", json={"title": "Locked"})).json()["id"]
    card_id = (await client.post("/api/characters", json={"name": "Lira"})).json()["id"]

    await client.put(f"/api/conversations/{cid}", json={"persona_lock_id": persona_id})
    await client.put(f"/api/characters/{card_id}", json={"persona_lock_id": persona_id})

    resp = await client.delete(f"/api/user-personas/{persona_id}")
    assert resp.status_code == 200

    async with db.execute("SELECT persona_lock_id FROM conversations WHERE id = ?", (cid,)) as cur:
        assert (await cur.fetchone())["persona_lock_id"] is None
    async with db.execute("SELECT persona_lock_id FROM character_cards WHERE id = ?", (card_id,)) as cur:
        assert (await cur.fetchone())["persona_lock_id"] is None
