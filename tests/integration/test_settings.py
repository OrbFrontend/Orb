from __future__ import annotations

import json


async def test_get_settings_returns_defaults(client, db):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["model_name"] == "default"
    assert data["temperature"] == 0.8
    assert isinstance(data["enabled_tools"], dict)


async def test_update_settings_persists_to_db(client, db):
    resp = await client.put(
        "/api/settings",
        json={"model_name": "my-model", "temperature": 0.5, "max_tokens": 1024},
    )
    assert resp.status_code == 200
    assert resp.json()["model_name"] == "my-model"

    # Verify directly in the DB
    async with db.execute(
        "SELECT model_name, temperature, max_tokens FROM settings WHERE id = 1"
    ) as cur:
        row = await cur.fetchone()
    assert row["model_name"] == "my-model"
    assert row["temperature"] == 0.5
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
