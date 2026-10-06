import json

import pytest

from backend.core import extract_hyperparams
from backend.database.queries.settings import get_settings
from backend.database.seeds import DEFAULT_CONNECTION
from backend.inference import EndpointConfigError, client_from_settings


async def test_get_settings_returns_defaults(client, db):
    data = await client.get_json("/api/settings")
    # model_name, temperature, max_tokens are now retrieved from the active endpoint's model config (seeded during init_db)
    assert "model_name" in data
    assert "temperature" in data
    assert isinstance(data["enabled_tools"], dict)
    assert data["expression_rendering"] == "classic"


async def test_expression_rendering_preference(client, db):
    await client.put_checked("/api/settings", json={"expression_rendering": "expression"})
    assert (await client.get_json("/api/settings"))["expression_rendering"] == "expression"
    assert (await client.put("/api/settings", json={"expression_rendering": "invalid"})).status_code == 422


async def test_update_settings_persists_to_db(client, db):
    # Update settings that are actually stored in the settings table
    # (model_name, temperature, max_tokens are now managed via endpoints/model_configs)
    resp = await client.put_json("/api/settings", json={"user_name": "TestUser", "user_description": "A test user"})
    assert resp["user_name"] == "TestUser"

    # Verify directly in the DB
    row = await db.one("SELECT user_name, user_description FROM settings WHERE id = 1")
    assert row["user_name"] == "TestUser"
    assert row["user_description"] == "A test user"


async def test_update_settings_ignores_connection_and_hyperparams(client):
    # The connection, model and hyperparameters live on the active endpoint and model_config, not the settings row. The frontend
    # still sends them to /settings; they are ignored there (mirroring completion_mode) and reads keep the overlaid values.
    before = await client.get_json("/api/settings")

    payload = {"endpoint_url": "http://elsewhere/v1", "api_key": "sk-x", "model_name": "other", "max_tokens": 1}
    after = await client.put_json("/api/settings", json=payload)

    assert {key: after.get(key) for key in payload} == {key: before.get(key) for key in payload}
    assert (await client.get_json(f"/api/endpoints/{after['active_endpoint_id']}/api-key"))["api_key"] != "sk-x"


async def test_connection_keys_survive_deleting_the_active_model_and_endpoint(client):
    endpoint_id = (await client.get_json("/api/settings"))["active_endpoint_id"]
    endpoint = await client.get_json(f"/api/endpoints/{endpoint_id}")

    await client.delete(f"/api/models/{endpoint['active_model_config_id']}")
    no_model = await get_settings()
    assert (no_model["endpoint_url"], no_model["model_name"]) == (endpoint["url"], "")
    assert no_model["max_tokens"] == DEFAULT_CONNECTION["max_tokens"]

    await client.delete(f"/api/endpoints/{endpoint_id}")
    no_endpoint = await get_settings()
    assert (no_endpoint["endpoint_url"], no_endpoint["api_key"], no_endpoint["model_name"]) == ("", "", "")
    with pytest.raises(EndpointConfigError):
        client_from_settings(no_endpoint)


async def test_update_settings_reflected_in_get(client, db):
    await client.put("/api/settings", json={"user_name": "Tester"})
    assert (await client.get("/api/settings")).json()["user_name"] == "Tester"


async def test_hyperparam_edit_via_model_config_reflected_in_get_settings(client, db):
    # The live path: hyperparams are edited on the active endpoint's model_config (PUT /api/models/{id}); get_settings()
    # overlays them so consumers see the new value. This is what replaces the removed /settings write path.
    endpoint_id = (await client.get_json("/api/settings"))["active_endpoint_id"]
    assert endpoint_id is not None
    active_mc_id = (await client.get_json(f"/api/endpoints/{endpoint_id}"))["active_model_config_id"]

    await client.put_checked(f"/api/models/{active_mc_id}", json={"max_tokens": 1234, "temperature": 0.33})

    updated = await client.get_json("/api/settings")
    assert updated["max_tokens"] == 1234
    assert updated["temperature"] == 0.33


async def test_null_hyperparam_overrides_the_legacy_setting_and_is_omitted(client):
    endpoint_id = (await client.get_json("/api/settings"))["active_endpoint_id"]
    active_mc_id = (await client.get_json(f"/api/endpoints/{endpoint_id}"))["active_model_config_id"]

    await client.put_checked(f"/api/models/{active_mc_id}", json={"temperature": None})

    updated = await get_settings()
    assert updated["temperature"] is None
    assert "temperature" not in extract_hyperparams(updated)


async def test_endpoint_proxy_overlay_and_client_threading(client):
    # Proxy lives on the endpoints row; get_settings() overlays it as settings["proxy"] (mirroring completion_mode) and
    # client_from_settings threads it into the LLMClient that talks to the endpoint.
    settings = await client.get_json("/api/settings")
    endpoint_id = settings["active_endpoint_id"]
    assert endpoint_id is not None
    assert settings.get("proxy", "") == ""
    assert client_from_settings(await get_settings()).proxy is None

    resp = await client.put_json(f"/api/endpoints/{endpoint_id}", json={"proxy": "socks5://127.0.0.1:1080"})
    assert resp["proxy"] == "socks5://127.0.0.1:1080"

    assert (await client.get_json("/api/settings"))["proxy"] == "socks5://127.0.0.1:1080"
    assert client_from_settings(await get_settings()).proxy == "socks5://127.0.0.1:1080"


async def test_endpoint_update_rejects_bad_proxy_scheme(client):
    # The EndpointUpdate scheme gate returns 422 on save, so a typo never reaches
    # the DB and never fails silently on an LLM turn.
    endpoint_id = (await client.get_json("/api/settings"))["active_endpoint_id"]
    await client.put_checked(f"/api/endpoints/{endpoint_id}", json={"proxy": "ftp://nope:1"}, expected_status=422)


async def test_update_enabled_tools_json_field(client, db):
    tools = {"direct_scene": True, "editor_apply_patch": False}
    assert (await client.put_json("/api/settings", json={"enabled_tools": tools}))["enabled_tools"] == tools

    row = await db.one("SELECT enabled_tools FROM settings WHERE id = 1")
    assert json.loads(row["enabled_tools"]) == tools


async def test_enabled_tools_sanitized_to_registered_tools(client, db):
    # Non-tool keys (the former length_guard* feature flags, or anything else not
    # in the tool catalog) must never be persisted back into enabled_tools.
    resp = await client.put_json(
        "/api/settings", json={"enabled_tools": {"direct_scene": True, "length_guard": True, "not_a_tool": True}}
    )
    assert resp["enabled_tools"] == {"direct_scene": True}

    row = await db.one("SELECT enabled_tools FROM settings WHERE id = 1")
    assert json.loads(row["enabled_tools"]) == {"direct_scene": True}


async def test_length_guard_flags_roundtrip(client, db):
    data = await client.put_json("/api/settings", json={"length_guard_enabled": True, "length_guard_enforce": True})
    assert data["length_guard_enabled"] == 1
    assert data["length_guard_enforce"] == 1

    row = await db.one("SELECT length_guard_enabled, length_guard_enforce FROM settings WHERE id = 1")
    assert row["length_guard_enabled"] == 1
    assert row["length_guard_enforce"] == 1


async def test_show_editor_diff_default_and_roundtrip(client, db):
    resp = await client.get_json("/api/settings")
    assert resp["show_editor_diff"] == 1

    resp = await client.put_json("/api/settings", json={"show_editor_diff": False})
    assert resp["show_editor_diff"] == 0

    assert (await db.one("SELECT show_editor_diff FROM settings WHERE id = 1"))["show_editor_diff"] == 0

    resp = await client.put("/api/settings", json={"show_editor_diff": True})
    assert resp.json()["show_editor_diff"] == 1


async def test_editor_audit_toggles_default_and_roundtrip(client, db):
    resp = await client.get_json("/api/settings")
    toggles = resp["editor_audit_toggles"]
    assert toggles == {
        "banned_phrases": True,
        "repetitive_openers": True,
        "repetitive_templates": True,
        "contrastive_negation": True,
        "phrase_repetition": True,
        "structural_repetition": True,
        "anti_echo": True,
        "negated_narration": False,
    }

    updated = {**toggles, "banned_phrases": False, "structural_repetition": False, "negated_narration": True}
    resp = await client.put_json("/api/settings", json={"editor_audit_toggles": updated})
    assert resp["editor_audit_toggles"] == updated

    row = await db.one("SELECT editor_audit_toggles FROM settings WHERE id = 1")
    assert json.loads(row["editor_audit_toggles"]) == updated


async def test_show_chat_avatars_default_and_roundtrip(client, db):
    resp = await client.get_json("/api/settings")
    assert resp["show_chat_avatars"] == 0

    resp = await client.put_json("/api/settings", json={"show_chat_avatars": True})
    assert resp["show_chat_avatars"] == 1

    assert (await db.one("SELECT show_chat_avatars FROM settings WHERE id = 1"))["show_chat_avatars"] == 1

    resp = await client.put("/api/settings", json={"show_chat_avatars": False})
    assert resp.json()["show_chat_avatars"] == 0


async def test_inspector_inline_default_and_roundtrip(client, db):
    assert (await client.get_json("/api/settings"))["inspector_inline"] == 0

    resp = await client.put_json("/api/settings", json={"inspector_inline": True})
    assert resp["inspector_inline"] == 1

    assert (await db.one("SELECT inspector_inline FROM settings WHERE id = 1"))["inspector_inline"] == 1

    resp = await client.put("/api/settings", json={"inspector_inline": False})
    assert resp.json()["inspector_inline"] == 0


async def test_hide_streaming_until_baked_default_and_roundtrip(client, db):
    resp = await client.get_json("/api/settings")
    assert resp["hide_streaming_until_baked"] == 0

    resp = await client.put_json("/api/settings", json={"hide_streaming_until_baked": True})
    assert resp["hide_streaming_until_baked"] == 1

    assert (await db.one("SELECT hide_streaming_until_baked FROM settings WHERE id = 1"))["hide_streaming_until_baked"] == 1

    resp = await client.put("/api/settings", json={"hide_streaming_until_baked": False})
    assert resp.json()["hide_streaming_until_baked"] == 0
