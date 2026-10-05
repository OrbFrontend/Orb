from __future__ import annotations

import json

import httpx

from backend.api.routes import endpoints as endpoint_routes


async def test_saved_keys_leave_the_server_only_through_the_reveal_route(client):
    key = "sk-or-v1-0123456789abcdef"
    endpoint = await client.post_json("/api/endpoints", json={"url": "https://masked.test/v1", "api_key": key})
    endpoint_id = endpoint["id"]
    await client.put_checked(
        "/api/settings",
        json={"active_endpoint_id": endpoint_id, "agent_endpoint_id": endpoint_id, "agent_same_as_writer": False},
    )

    responses = [
        endpoint,
        await client.put_json(f"/api/endpoints/{endpoint_id}", json={"proxy": ""}),
        await client.get_json(f"/api/endpoints/{endpoint_id}"),
        await client.get_json("/api/endpoints"),
        await client.get_json("/api/settings"),
        await client.put_json("/api/settings", json={"user_name": "Masked"}),
    ]

    assert all(key not in json.dumps(body) for body in responses)
    assert endpoint["api_key_hint"] == "••••••••cdef"
    assert endpoint_routes.api_key_hint("short-key") == "••••••••"
    assert await client.get_json(f"/api/endpoints/{endpoint_id}/api-key") == {"api_key": key}


async def test_discover_available_models_uses_saved_endpoint(client, monkeypatch):
    endpoint = (await client.post("/api/endpoints", json={"url": "https://catalog.test/v1", "api_key": "catalog-key"})).json()
    await client.put(f"/api/endpoints/{endpoint['id']}", json={"proxy": "http://localhost:8080"})
    seen = {}

    class FakeLLMClient:
        def __init__(self, base_url, api_key, *, proxy):
            seen.update(base_url=base_url, api_key=api_key, proxy=proxy)

        async def list_models(self):
            return ["model/a", "model/b"]

    monkeypatch.setattr(endpoint_routes, "LLMClient", FakeLLMClient)

    resp = await client.get_json(f"/api/endpoints/{endpoint['id']}/available-models")

    assert resp == {"models": ["model/a", "model/b"]}
    assert seen == {"base_url": "https://catalog.test/v1", "api_key": "catalog-key", "proxy": "http://localhost:8080"}


async def test_discover_available_models_surfaces_provider_error_without_key(client, monkeypatch):
    endpoint = (
        await client.post("/api/endpoints", json={"url": "https://catalog.test/v1", "api_key": "secret-catalog-key"})
    ).json()

    class FakeLLMClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def list_models(self):
            request = httpx.Request("GET", "https://catalog.test/v1/models")
            response = httpx.Response(401, json={"error": {"message": "Bad key: secret-catalog-key"}}, request=request)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(endpoint_routes, "LLMClient", FakeLLMClient)

    resp = await client.get_json(f"/api/endpoints/{endpoint['id']}/available-models", expected_status=502)

    assert resp["detail"] == "Model discovery failed (provider HTTP 401): Bad key: [redacted]"


async def test_delete_endpoint_removes_from_db(client, db):
    # Create an endpoint
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.delete.com", "api_key": "key"})

    # Verify it exists
    row = await db.one("SELECT COUNT(*) as count FROM endpoints WHERE id = ?", (endpoint_id,))
    assert row["count"] == 1

    # Delete the endpoint
    delete_resp = await client.delete_json(f"/api/endpoints/{endpoint_id}")
    assert delete_resp == {"ok": True}

    # Verify it's gone from DB
    row = await db.one("SELECT COUNT(*) as count FROM endpoints WHERE id = ?", (endpoint_id,))
    assert row["count"] == 0


async def test_delete_nonexistent_endpoint_returns_error(client, db):
    resp = await client.delete("/api/endpoints/99999")
    # Should return 404 or 400 depending on implementation
    assert resp.status_code in (404, 400)


async def test_create_model_config_persists_to_db(client, db):
    # First create an endpoint
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.models.com", "api_key": "key"})

    # Create a model config for this endpoint
    resp = await client.post_json(
        f"/api/endpoints/{endpoint_id}/models",
        json={"model_name": "test-model-1", "system_prompt": "You are a test model.", "temperature": 0.7, "max_tokens": 2048},
    )
    data = resp
    assert "id" in data
    assert data["model_name"] == "test-model-1"
    assert data["endpoint_id"] == endpoint_id
    assert data["temperature"] == 0.7
    assert data["max_tokens"] == 2048

    # Verify directly in the DB
    row = await db.one(
        "SELECT model_name, system_prompt, temperature, max_tokens FROM model_configs WHERE id = ?", (data["id"],)
    )
    assert row["model_name"] == "test-model-1"
    assert row["system_prompt"] == "You are a test model."
    assert row["temperature"] == 0.7
    assert row["max_tokens"] == 2048


async def test_model_config_hyperparameters_can_be_explicitly_null(client, db):
    endpoint = (await client.post("/api/endpoints", json={"url": "https://api.nullable.test"})).json()
    config = await client.post_checked(
        f"/api/endpoints/{endpoint['id']}/models",
        json={"model_name": "provider-defaults", "temperature": None, "max_tokens": None},
    )
    assert config.json()["temperature"] is None
    assert config.json()["max_tokens"] is None

    updated = await client.put_json(f"/api/models/{config.json()['id']}", json={"top_p": None})
    assert updated["top_p"] is None

    row = await db.one("SELECT temperature, top_p, max_tokens FROM model_configs WHERE id = ?", (config.json()["id"],))
    assert dict(row) == {"temperature": None, "top_p": None, "max_tokens": None}


async def test_create_model_config_rejects_unknown_role(client):
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.invalid-role.com", "api_key": "key"})

    await client.post_checked(
        f"/api/endpoints/{endpoint_id}/models", json={"model_name": "invalid-role", "role": "critic"}, expected_status=422
    )


async def test_list_model_configs_for_endpoint(client, db):
    # Create an endpoint
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.list.com", "api_key": "key"})

    # Create two model configs
    await client.post(f"/api/endpoints/{endpoint_id}/models", json={"model_name": "model-a", "temperature": 0.5})
    await client.post(f"/api/endpoints/{endpoint_id}/models", json={"model_name": "model-b", "temperature": 0.9})

    # List model configs for this endpoint
    resp = await client.get_json(f"/api/endpoints/{endpoint_id}/models")
    configs = resp

    assert len(configs) >= 2  # Could have default configs

    # Check our created configs exist
    model_names = [c["model_name"] for c in configs]
    assert "model-a" in model_names
    assert "model-b" in model_names


async def test_delete_model_config_removes_from_db(client, db):
    # Create endpoint and model config
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.delete-model.com", "api_key": "key"})

    config_id = await client.create(
        f"/api/endpoints/{endpoint_id}/models", json={"model_name": "to-delete", "temperature": 0.5}
    )

    # Verify it exists
    row = await db.one("SELECT COUNT(*) as count FROM model_configs WHERE id = ?", (config_id,))
    assert row["count"] == 1

    # Delete the model config
    delete_resp = await client.delete_json(f"/api/models/{config_id}")
    assert delete_resp == {"ok": True}

    # Verify it's gone from DB
    row = await db.one("SELECT COUNT(*) as count FROM model_configs WHERE id = ?", (config_id,))
    assert row["count"] == 0


async def test_cannot_create_model_config_for_nonexistent_endpoint(client, db):
    resp = await client.post("/api/endpoints/99999/models", json={"model_name": "test", "temperature": 0.5})
    # Should return 404 or 400
    assert resp.status_code in (404, 400)


async def test_model_config_reasoning_effort_round_trip(client, db):
    """reasoning_effort trio persists through create and update."""
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.reasoning.com", "api_key": "key"})

    resp = await client.post_json(
        f"/api/endpoints/{endpoint_id}/models",
        json={
            "model_name": "thinking-model",
            "reasoning_effort": "custom",
            "reasoning_effort_param": "thinking_budget",
            "reasoning_effort_value": "4096",
        },
    )
    data = resp
    assert data["reasoning_effort"] == "custom"
    assert data["reasoning_effort_param"] == "thinking_budget"
    assert data["reasoning_effort_value"] == "4096"

    update_resp = await client.put_json(f"/api/models/{data['id']}", json={"reasoning_effort": "xhigh"})
    assert update_resp["reasoning_effort"] == "xhigh"

    row = await db.one("SELECT reasoning_effort, reasoning_effort_param FROM model_configs WHERE id = ?", (data["id"],))
    assert row["reasoning_effort"] == "xhigh"
    assert row["reasoning_effort_param"] == "thinking_budget"


async def test_settings_overlay_reasoning_effort(client, db):
    """The active model config's reasoning settings -- including a custom param
    name and its value -- reach get_settings, and the agent lane inherits them
    when it shares the writer's endpoint."""
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.overlay.com", "api_key": "key"})
    config_id = await client.create(
        f"/api/endpoints/{endpoint_id}/models",
        json={
            "model_name": "overlay-model",
            "reasoning_effort": "custom",
            "reasoning_effort_param": "reasoning_effort",
            "reasoning_effort_value": "max",
        },
    )

    await client.put("/api/settings", json={"active_endpoint_id": endpoint_id, "agent_same_as_writer": True})
    await client.put(f"/api/endpoints/{endpoint_id}", json={"active_model_config_id": config_id})

    resp = await client.get_json("/api/settings")
    settings = resp
    assert settings["reasoning_effort"] == "custom"
    assert settings["agent_reasoning_effort"] == "custom"
    assert settings["reasoning_effort_param"] == "reasoning_effort"
    assert settings["reasoning_effort_value"] == "max"
    assert settings["agent_reasoning_effort_value"] == "max"


async def test_model_config_extra_request_round_trip(client, db):
    """extra_headers/extra_body persist through create and update."""
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.extra.com", "api_key": "key"})

    resp = await client.post_json(
        f"/api/endpoints/{endpoint_id}/models",
        json={
            "model_name": "routed-model",
            "extra_headers": "X-Provider: deepinfra",
            "extra_body": '{"provider": {"only": ["deepinfra"]}}',
        },
    )
    data = resp
    assert data["extra_headers"] == "X-Provider: deepinfra"
    assert data["extra_body"] == '{"provider": {"only": ["deepinfra"]}}'

    update_resp = await client.put_json(f"/api/models/{data['id']}", json={"extra_headers": "X-Provider: together"})
    assert update_resp["extra_headers"] == "X-Provider: together"

    row = await db.one("SELECT extra_headers, extra_body FROM model_configs WHERE id = ?", (data["id"],))
    assert row["extra_headers"] == "X-Provider: together"
    assert row["extra_body"] == '{"provider": {"only": ["deepinfra"]}}'


async def test_settings_overlay_extra_request(client, db):
    """The active model config's extra fields reach get_settings, and the agent
    inherits them when sharing the writer endpoint."""
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.extraoverlay.com", "api_key": "key"})
    config_id = await client.create(
        f"/api/endpoints/{endpoint_id}/models",
        json={"model_name": "overlay-routed", "extra_headers": "X-Provider: deepinfra", "extra_body": '{"seed": 7}'},
    )

    await client.put("/api/settings", json={"active_endpoint_id": endpoint_id, "agent_same_as_writer": True})
    await client.put(f"/api/endpoints/{endpoint_id}", json={"active_model_config_id": config_id})

    resp = await client.get_json("/api/settings")
    settings = resp
    assert settings["extra_headers"] == "X-Provider: deepinfra"
    assert settings["extra_body"] == '{"seed": 7}'
    assert settings["agent_extra_headers"] == "X-Provider: deepinfra"
    assert settings["agent_extra_body"] == '{"seed": 7}'


async def test_model_config_rejects_malformed_extra_body(client, db):
    """A create payload is complete apart from the bad field, so the 422 can only come from the extra_body validator."""
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://api.extrareject.com", "api_key": "key"})

    await client.post_checked(
        f"/api/endpoints/{endpoint_id}/models", json={"model_name": "bad-model", "extra_body": "{nope"}, expected_status=422
    )


async def test_endpoint_crud_workflow(client, db):
    # 1. Create endpoint
    endpoint_id = await client.create("/api/endpoints", json={"url": "https://workflow.example.com", "api_key": "workflow-key"})

    # 2. Verify in list
    list_resp = await client.get_json("/api/endpoints")
    endpoints = list_resp
    assert any(e["id"] == endpoint_id for e in endpoints)

    # 3. Create model config for endpoint
    config_id = await client.create(
        f"/api/endpoints/{endpoint_id}/models", json={"model_name": "workflow-model", "temperature": 0.6}
    )

    # 4. Verify model config in list
    models_resp = await client.get_json(f"/api/endpoints/{endpoint_id}/models")
    models = models_resp
    assert any(m["id"] == config_id for m in models)

    # 5. Delete model config
    await client.delete_checked(f"/api/models/{config_id}")

    # 6. Verify model config deleted
    models_resp2 = await client.get_json(f"/api/endpoints/{endpoint_id}/models")
    models2 = models_resp2
    assert not any(m["id"] == config_id for m in models2)

    # 7. Delete endpoint
    await client.delete_checked(f"/api/endpoints/{endpoint_id}")

    # 8. Verify endpoint deleted
    list_resp2 = await client.get_json("/api/endpoints")
    endpoints2 = list_resp2
    assert not any(e["id"] == endpoint_id for e in endpoints2)
