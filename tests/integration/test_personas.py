from __future__ import annotations


async def test_create_persona_persists_to_db(client, db):
    resp = await client.post(
        "/api/user-personas",
        json={
            "name": "Alice",
            "description": "The main player.",
            "avatar_color": "#ff0000",
        },
    )
    assert resp.status_code == 200
    persona_id = resp.json()["id"]

    async with db.execute(
        "SELECT name, description, avatar_color FROM user_personas WHERE id = ?",
        (persona_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["name"] == "Alice"
    assert row["description"] == "The main player."
    assert row["avatar_color"] == "#ff0000"


async def test_list_personas_includes_created(client, db):
    await client.post("/api/user-personas", json={"name": "Bob"})
    resp = await client.get("/api/user-personas")
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert "Bob" in names


async def test_update_persona_persists_to_db(client, db):
    create_resp = await client.post("/api/user-personas", json={"name": "OldName"})
    persona_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/user-personas/{persona_id}",
        json={"name": "NewName", "description": "Updated."},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "NewName"

    async with db.execute(
        "SELECT name, description FROM user_personas WHERE id = ?", (persona_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["name"] == "NewName"
    assert row["description"] == "Updated."


async def test_delete_persona_removes_from_db(client, db):
    create_resp = await client.post("/api/user-personas", json={"name": "Temporary"})
    persona_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/user-personas/{persona_id}")
    assert resp.status_code == 200

    async with db.execute(
        "SELECT id FROM user_personas WHERE id = ?", (persona_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is None


async def test_delete_nonexistent_persona_returns_404(client, db):
    resp = await client.delete("/api/user-personas/99999")
    assert resp.status_code == 404


async def test_update_nonexistent_persona_returns_404(client, db):
    resp = await client.put("/api/user-personas/99999", json={"name": "Ghost"})
    assert resp.status_code == 404
