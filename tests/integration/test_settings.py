from __future__ import annotations

import json


async def test_get_settings_returns_defaults(client, db):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    # model_name, temperature, max_tokens are now retrieved from the active
    # endpoint's model config (seeded during init_db)
    assert "model_name" in data
    assert "temperature" in data
    assert isinstance(data["enabled_tools"], dict)


async def test_update_settings_persists_to_db(client, db):
    # Update settings that are actually stored in the settings table
    # (model_name, temperature, max_tokens are now managed via endpoints/model_configs)
    resp = await client.put(
        "/api/settings",
        json={"user_name": "TestUser", "user_description": "A test user", "max_tokens": 1024},
    )
    assert resp.status_code == 200
    assert resp.json()["user_name"] == "TestUser"

    # Verify directly in the DB
    async with db.execute(
        "SELECT user_name, user_description, max_tokens FROM settings WHERE id = 1"
    ) as cur:
        row = await cur.fetchone()
    assert row["user_name"] == "TestUser"
    assert row["user_description"] == "A test user"
    # Note: max_tokens may be overridden by active model config in get_settings()
    # but is stored in settings table when updated via settings API
    assert row["max_tokens"] == 1024


async def test_update_settings_reflected_in_get(client, db):
    await client.put("/api/settings", json={"user_name": "Tester"})
    resp = await client.get("/api/settings")
    assert resp.json()["user_name"] == "Tester"


async def test_update_enabled_tools_json_field(client, db):
    tools = {"direct_scene": True, "rewrite_user_prompt": False}
    resp = await client.put("/api/settings", json={"enabled_tools": tools})
    assert resp.status_code == 200
    assert resp.json()["enabled_tools"] == tools

    async with db.execute("SELECT enabled_tools FROM settings WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert json.loads(row["enabled_tools"]) == tools
