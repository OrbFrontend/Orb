from __future__ import annotations

import base64

PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


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

    async with db.execute("SELECT name, description FROM user_personas WHERE id = ?", (persona_id,)) as cur:
        row = await cur.fetchone()
    assert row["name"] == "NewName"
    assert row["description"] == "Updated."


async def test_delete_persona_removes_from_db(client, db):
    create_resp = await client.post("/api/user-personas", json={"name": "Temporary"})
    persona_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/user-personas/{persona_id}")
    assert resp.status_code == 200

    async with db.execute("SELECT id FROM user_personas WHERE id = ?", (persona_id,)) as cur:
        row = await cur.fetchone()
    assert row is None


async def test_delete_nonexistent_persona_returns_404(client, db):
    resp = await client.delete("/api/user-personas/99999")
    assert resp.status_code == 404


async def test_update_nonexistent_persona_returns_404(client, db):
    resp = await client.put("/api/user-personas/99999", json={"name": "Ghost"})
    assert resp.status_code == 404


async def test_create_persona_with_avatar_stores_and_serves_it(client, db):
    resp = await client.post(
        "/api/user-personas",
        json={"name": "Pictured", "avatar_b64": PNG_B64, "avatar_mime": "image/png"},
    )
    assert resp.status_code == 200
    persona_id = resp.json()["id"]

    async with db.execute("SELECT avatar_b64, avatar_mime FROM user_personas WHERE id = ?", (persona_id,)) as cur:
        row = await cur.fetchone()
    assert row["avatar_b64"] == PNG_B64
    assert row["avatar_mime"] == "image/png"

    resp = await client.get(f"/api/user-personas/{persona_id}/avatar")
    assert resp.status_code == 200
    assert resp.content == PNG_BYTES
    assert resp.headers["content-type"].startswith("image/png")
    etag = resp.headers["etag"]
    assert etag

    resp = await client.get(f"/api/user-personas/{persona_id}/avatar", headers={"If-None-Match": etag})
    assert resp.status_code == 304
    assert resp.headers["etag"] == etag


async def test_list_personas_reports_has_avatar_without_the_blob(client, db):
    await client.post("/api/user-personas", json={"name": "Pictured", "avatar_b64": PNG_B64, "avatar_mime": "image/png"})
    await client.post("/api/user-personas", json={"name": "Plain"})

    resp = await client.get("/api/user-personas")
    assert resp.status_code == 200
    by_name = {p["name"]: p for p in resp.json()}
    assert by_name["Pictured"]["has_avatar"] is True
    assert by_name["Plain"]["has_avatar"] is False
    assert "avatar_b64" not in by_name["Pictured"]
    assert PNG_B64 not in resp.text


async def test_persona_without_avatar_returns_404(client, db):
    persona_id = (await client.post("/api/user-personas", json={"name": "Plain"})).json()["id"]
    assert (await client.get(f"/api/user-personas/{persona_id}/avatar")).status_code == 404


async def test_explicit_null_clears_a_persona_avatar(client, db):
    persona_id = (
        await client.post(
            "/api/user-personas",
            json={"name": "Pictured", "avatar_b64": PNG_B64, "avatar_mime": "image/png"},
        )
    ).json()["id"]

    resp = await client.put(f"/api/user-personas/{persona_id}", json={"avatar_b64": None, "avatar_mime": None})
    assert resp.status_code == 200
    assert resp.json()["has_avatar"] is False

    async with db.execute("SELECT avatar_b64, avatar_mime FROM user_personas WHERE id = ?", (persona_id,)) as cur:
        row = await cur.fetchone()
    assert row["avatar_b64"] is None
    assert row["avatar_mime"] is None
    assert (await client.get(f"/api/user-personas/{persona_id}/avatar")).status_code == 404


async def test_update_without_avatar_keys_leaves_the_image_alone(client, db):
    persona_id = (
        await client.post(
            "/api/user-personas",
            json={"name": "Pictured", "avatar_b64": PNG_B64, "avatar_mime": "image/png"},
        )
    ).json()["id"]

    resp = await client.put(f"/api/user-personas/{persona_id}", json={"name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["has_avatar"] is True
    assert (await client.get(f"/api/user-personas/{persona_id}/avatar")).status_code == 200


async def test_invalid_base64_avatar_is_rejected(client, db):
    resp = await client.post("/api/user-personas", json={"name": "Bad", "avatar_b64": "not base64!!"})
    assert resp.status_code == 422


async def test_oversized_avatar_is_rejected(client, db):
    oversized = base64.b64encode(b"\x00" * (2 * 1024 * 1024 + 1)).decode()
    resp = await client.post("/api/user-personas", json={"name": "Huge", "avatar_b64": oversized})
    assert resp.status_code == 422
