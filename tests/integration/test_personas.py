from __future__ import annotations

import base64

PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


async def test_create_persona_persists_to_db(client, db):
    persona_id = await client.create(
        "/api/user-personas", json={"name": "Alice", "description": "The main player.", "avatar_color": "#ff0000"}
    )

    row = await db.one("SELECT name, description, avatar_color FROM user_personas WHERE id = ?", (persona_id,))
    assert row["name"] == "Alice"
    assert row["description"] == "The main player."
    assert row["avatar_color"] == "#ff0000"


async def test_list_personas_includes_created(client, db):
    await client.post("/api/user-personas", json={"name": "Bob"})
    resp = await client.get_json("/api/user-personas")
    names = [p["name"] for p in resp]
    assert "Bob" in names


async def test_update_persona_persists_to_db(client, db):
    persona_id = await client.create("/api/user-personas", json={"name": "OldName"})

    resp = await client.put_json(f"/api/user-personas/{persona_id}", json={"name": "NewName", "description": "Updated."})
    assert resp["name"] == "NewName"

    row = await db.one("SELECT name, description FROM user_personas WHERE id = ?", (persona_id,))
    assert row["name"] == "NewName"
    assert row["description"] == "Updated."


async def test_delete_persona_removes_from_db(client, db):
    persona_id = await client.create("/api/user-personas", json={"name": "Temporary"})

    await client.delete_checked(f"/api/user-personas/{persona_id}")

    row = await db.one("SELECT id FROM user_personas WHERE id = ?", (persona_id,))
    assert row is None


async def test_delete_nonexistent_persona_returns_404(client, db):
    await client.delete_checked("/api/user-personas/99999", expected_status=404)


async def test_update_nonexistent_persona_returns_404(client, db):
    await client.put_checked("/api/user-personas/99999", json={"name": "Ghost"}, expected_status=404)


async def test_create_persona_with_avatar_stores_and_serves_it(client, db):
    persona_id = await client.create(
        "/api/user-personas", json={"name": "Pictured", "avatar_b64": PNG_B64, "avatar_mime": "image/png"}
    )

    row = await db.one("SELECT avatar_b64, avatar_mime FROM user_personas WHERE id = ?", (persona_id,))
    assert row["avatar_b64"] == PNG_B64
    assert row["avatar_mime"] == "image/png"

    resp = await client.get_checked(f"/api/user-personas/{persona_id}/avatar")
    assert resp.content == PNG_BYTES
    assert resp.headers["content-type"].startswith("image/png")
    etag = resp.headers["etag"]
    assert etag

    resp = await client.get_checked(
        f"/api/user-personas/{persona_id}/avatar", headers={"If-None-Match": etag}, expected_status=304
    )
    assert resp.headers["etag"] == etag


async def test_list_personas_reports_has_avatar_without_the_blob(client, db):
    await client.post("/api/user-personas", json={"name": "Pictured", "avatar_b64": PNG_B64, "avatar_mime": "image/png"})
    await client.post("/api/user-personas", json={"name": "Plain"})

    resp = await client.get_checked("/api/user-personas")
    by_name = {p["name"]: p for p in resp.json()}
    assert by_name["Pictured"]["has_avatar"] is True
    assert by_name["Plain"]["has_avatar"] is False
    assert "avatar_b64" not in by_name["Pictured"]
    assert PNG_B64 not in resp.text


async def test_persona_without_avatar_returns_404(client, db):
    persona_id = await client.create("/api/user-personas", json={"name": "Plain"})
    assert (await client.get(f"/api/user-personas/{persona_id}/avatar")).status_code == 404


async def test_explicit_null_clears_a_persona_avatar(client, db):
    persona_id = await client.create(
        "/api/user-personas", json={"name": "Pictured", "avatar_b64": PNG_B64, "avatar_mime": "image/png"}
    )

    resp = await client.put_json(f"/api/user-personas/{persona_id}", json={"avatar_b64": None, "avatar_mime": None})
    assert resp["has_avatar"] is False

    row = await db.one("SELECT avatar_b64, avatar_mime FROM user_personas WHERE id = ?", (persona_id,))
    assert row["avatar_b64"] is None
    assert row["avatar_mime"] is None
    assert (await client.get(f"/api/user-personas/{persona_id}/avatar")).status_code == 404


async def test_update_without_avatar_keys_leaves_the_image_alone(client, db):
    persona_id = await client.create(
        "/api/user-personas", json={"name": "Pictured", "avatar_b64": PNG_B64, "avatar_mime": "image/png"}
    )

    resp = await client.put_json(f"/api/user-personas/{persona_id}", json={"name": "Renamed"})
    assert resp["has_avatar"] is True
    assert (await client.get(f"/api/user-personas/{persona_id}/avatar")).status_code == 200


async def test_invalid_base64_avatar_is_rejected(client, db):
    await client.post_checked("/api/user-personas", json={"name": "Bad", "avatar_b64": "not base64!!"}, expected_status=422)


async def test_oversized_avatar_is_rejected(client, db):
    oversized = base64.b64encode(b"\x00" * (2 * 1024 * 1024 + 1)).decode()
    await client.post_checked("/api/user-personas", json={"name": "Huge", "avatar_b64": oversized}, expected_status=422)
