from __future__ import annotations

import json

from backend.database.queries.settings import get_settings
from backend.inference import client_from_settings


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
        json={
            "user_name": "TestUser",
            "user_description": "A test user",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["user_name"] == "TestUser"

    # Verify directly in the DB
    async with db.execute("SELECT user_name, user_description FROM settings WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert row["user_name"] == "TestUser"
    assert row["user_description"] == "A test user"


async def test_update_settings_ignores_hyperparams(client, db):
    # Hyperparameters live on the active model_config, not the settings row.
    # A /settings PUT that includes them must not touch the settings table's
    # flat columns (extra fields are ignored, mirroring completion_mode).
    async with db.execute("SELECT max_tokens FROM settings WHERE id = 1") as cur:
        before = (await cur.fetchone())["max_tokens"]

    resp = await client.put("/api/settings", json={"max_tokens": before + 1000})
    assert resp.status_code == 200

    async with db.execute("SELECT max_tokens FROM settings WHERE id = 1") as cur:
        after = (await cur.fetchone())["max_tokens"]
    assert after == before


async def test_update_settings_reflected_in_get(client, db):
    await client.put("/api/settings", json={"user_name": "Tester"})
    resp = await client.get("/api/settings")
    assert resp.json()["user_name"] == "Tester"


async def test_hyperparam_edit_via_model_config_reflected_in_get_settings(client, db):
    # The live path: hyperparams are edited on the active endpoint's model_config
    # (PUT /api/models/{id}); get_settings() overlays them so consumers see the
    # new value. This is what replaces the removed /settings write path.
    settings = (await client.get("/api/settings")).json()
    endpoint_id = settings["active_endpoint_id"]
    assert endpoint_id is not None
    active_mc_id = (await client.get(f"/api/endpoints/{endpoint_id}")).json()["active_model_config_id"]

    resp = await client.put(f"/api/models/{active_mc_id}", json={"max_tokens": 1234, "temperature": 0.33})
    assert resp.status_code == 200

    updated = (await client.get("/api/settings")).json()
    assert updated["max_tokens"] == 1234
    assert updated["temperature"] == 0.33


async def test_endpoint_proxy_overlay_and_client_threading(client):
    # Proxy lives on the endpoints row; get_settings() overlays it as
    # settings["proxy"] (mirroring completion_mode) and client_from_settings
    # threads it into the LLMClient that talks to the endpoint.
    settings = (await client.get("/api/settings")).json()
    endpoint_id = settings["active_endpoint_id"]
    assert endpoint_id is not None
    assert settings.get("proxy", "") == ""
    assert client_from_settings(await get_settings()).proxy is None

    resp = await client.put(f"/api/endpoints/{endpoint_id}", json={"proxy": "socks5://127.0.0.1:1080"})
    assert resp.status_code == 200
    assert resp.json()["proxy"] == "socks5://127.0.0.1:1080"

    updated = (await client.get("/api/settings")).json()
    assert updated["proxy"] == "socks5://127.0.0.1:1080"
    assert client_from_settings(await get_settings()).proxy == "socks5://127.0.0.1:1080"


async def test_endpoint_update_rejects_bad_proxy_scheme(client):
    # The EndpointUpdate scheme gate returns 422 on save, so a typo never reaches
    # the DB and never fails silently on an LLM turn.
    endpoint_id = (await client.get("/api/settings")).json()["active_endpoint_id"]
    resp = await client.put(f"/api/endpoints/{endpoint_id}", json={"proxy": "ftp://nope:1"})
    assert resp.status_code == 422


async def test_update_enabled_tools_json_field(client, db):
    tools = {"direct_scene": True, "editor_apply_patch": False}
    resp = await client.put("/api/settings", json={"enabled_tools": tools})
    assert resp.status_code == 200
    assert resp.json()["enabled_tools"] == tools

    async with db.execute("SELECT enabled_tools FROM settings WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert json.loads(row["enabled_tools"]) == tools


async def test_enabled_tools_sanitized_to_registered_tools(client, db):
    # Non-tool keys (the former length_guard* feature flags, or anything else not
    # in the tool registry) must never be persisted back into enabled_tools.
    resp = await client.put(
        "/api/settings",
        json={"enabled_tools": {"direct_scene": True, "length_guard": True, "not_a_tool": True}},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled_tools"] == {"direct_scene": True}

    async with db.execute("SELECT enabled_tools FROM settings WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert json.loads(row["enabled_tools"]) == {"direct_scene": True}


async def test_length_guard_flags_roundtrip(client, db):
    resp = await client.put(
        "/api/settings",
        json={"length_guard_enabled": True, "length_guard_enforce": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["length_guard_enabled"] == 1
    assert data["length_guard_enforce"] == 1

    async with db.execute("SELECT length_guard_enabled, length_guard_enforce FROM settings WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert row["length_guard_enabled"] == 1
    assert row["length_guard_enforce"] == 1


async def test_show_editor_diff_default_and_roundtrip(client, db):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["show_editor_diff"] == 1

    resp = await client.put("/api/settings", json={"show_editor_diff": False})
    assert resp.status_code == 200
    assert resp.json()["show_editor_diff"] == 0

    async with db.execute("SELECT show_editor_diff FROM settings WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert row["show_editor_diff"] == 0

    resp = await client.put("/api/settings", json={"show_editor_diff": True})
    assert resp.json()["show_editor_diff"] == 1


async def test_editor_audit_toggles_default_and_roundtrip(client, db):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    toggles = resp.json()["editor_audit_toggles"]
    assert toggles == {
        "banned_phrases": True,
        "repetitive_openers": True,
        "repetitive_templates": True,
        "contrastive_negation": True,
        "phrase_repetition": True,
        "structural_repetition": True,
        "anti_echo": True,
    }

    updated = {**toggles, "banned_phrases": False, "structural_repetition": False}
    resp = await client.put("/api/settings", json={"editor_audit_toggles": updated})
    assert resp.status_code == 200
    assert resp.json()["editor_audit_toggles"] == updated

    async with db.execute("SELECT editor_audit_toggles FROM settings WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert json.loads(row["editor_audit_toggles"]) == updated


async def test_hide_streaming_until_baked_default_and_roundtrip(client, db):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["hide_streaming_until_baked"] == 0

    resp = await client.put("/api/settings", json={"hide_streaming_until_baked": True})
    assert resp.status_code == 200
    assert resp.json()["hide_streaming_until_baked"] == 1

    async with db.execute("SELECT hide_streaming_until_baked FROM settings WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert row["hide_streaming_until_baked"] == 1

    resp = await client.put("/api/settings", json={"hide_streaming_until_baked": False})
    assert resp.json()["hide_streaming_until_baked"] == 0
