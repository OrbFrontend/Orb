from __future__ import annotations


async def test_create_endpoint_persists_to_db(client, db):
    """Test creating an endpoint via POST /api/endpoints"""
    resp = await client.post(
        "/api/endpoints",
        json={"url": "https://api.example.com/v1", "api_key": "test-key-123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["url"] == "https://api.example.com/v1"
    # API key should not be returned in response for security
    assert "api_key" not in data

    # Verify directly in the DB
    async with db.execute(
        "SELECT url, api_key FROM endpoints WHERE id = ?",
        (data["id"],),
    ) as cur:
        row = await cur.fetchone()
    assert row["url"] == "https://api.example.com/v1"
    assert row["api_key"] == "test-key-123"


async def test_delete_endpoint_removes_from_db(client, db):
    """Test DELETE /api/endpoints/{id} removes endpoint"""
    # Create an endpoint
    create_resp = await client.post(
        "/api/endpoints",
        json={"url": "https://api.delete.com", "api_key": "key"},
    )
    assert create_resp.status_code == 200
    endpoint_id = create_resp.json()["id"]

    # Verify it exists
    async with db.execute(
        "SELECT COUNT(*) as count FROM endpoints WHERE id = ?",
        (endpoint_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["count"] == 1

    # Delete the endpoint
    delete_resp = await client.delete(f"/api/endpoints/{endpoint_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"ok": True}

    # Verify it's gone from DB
    async with db.execute(
        "SELECT COUNT(*) as count FROM endpoints WHERE id = ?",
        (endpoint_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["count"] == 0


async def test_delete_nonexistent_endpoint_returns_error(client, db):
    """Test deleting a non-existent endpoint returns appropriate error"""
    resp = await client.delete("/api/endpoints/99999")
    # Should return 404 or 400 depending on implementation
    assert resp.status_code in (404, 400)


async def test_create_model_config_persists_to_db(client, db):
    """Test creating a model config via POST /api/endpoints/{id}/models"""
    # First create an endpoint
    endpoint_resp = await client.post(
        "/api/endpoints",
        json={"url": "https://api.models.com", "api_key": "key"},
    )
    assert endpoint_resp.status_code == 200
    endpoint_id = endpoint_resp.json()["id"]

    # Create a model config for this endpoint
    resp = await client.post(
        f"/api/endpoints/{endpoint_id}/models",
        json={
            "model_name": "test-model-1",
            "system_prompt": "You are a test model.",
            "temperature": 0.7,
            "max_tokens": 2048,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["model_name"] == "test-model-1"
    assert data["endpoint_id"] == endpoint_id
    assert data["temperature"] == 0.7
    assert data["max_tokens"] == 2048

    # Verify directly in the DB
    async with db.execute(
        "SELECT model_name, system_prompt, temperature, max_tokens FROM model_configs WHERE id = ?",
        (data["id"],),
    ) as cur:
        row = await cur.fetchone()
    assert row["model_name"] == "test-model-1"
    assert row["system_prompt"] == "You are a test model."
    assert row["temperature"] == 0.7
    assert row["max_tokens"] == 2048


async def test_list_model_configs_for_endpoint(client, db):
    """Test GET /api/endpoints/{id}/models returns model configs"""
    # Create an endpoint
    endpoint_resp = await client.post(
        "/api/endpoints",
        json={"url": "https://api.list.com", "api_key": "key"},
    )
    assert endpoint_resp.status_code == 200
    endpoint_id = endpoint_resp.json()["id"]

    # Create two model configs
    await client.post(
        f"/api/endpoints/{endpoint_id}/models",
        json={"model_name": "model-a", "temperature": 0.5},
    )
    await client.post(
        f"/api/endpoints/{endpoint_id}/models",
        json={"model_name": "model-b", "temperature": 0.9},
    )

    # List model configs for this endpoint
    resp = await client.get(f"/api/endpoints/{endpoint_id}/models")
    assert resp.status_code == 200
    configs = resp.json()

    assert len(configs) >= 2  # Could have default configs

    # Check our created configs exist
    model_names = [c["model_name"] for c in configs]
    assert "model-a" in model_names
    assert "model-b" in model_names


async def test_delete_model_config_removes_from_db(client, db):
    """Test DELETE /api/models/{config_id} removes model config"""
    # Create endpoint and model config
    endpoint_resp = await client.post(
        "/api/endpoints",
        json={"url": "https://api.delete-model.com", "api_key": "key"},
    )
    assert endpoint_resp.status_code == 200
    endpoint_id = endpoint_resp.json()["id"]

    model_resp = await client.post(
        f"/api/endpoints/{endpoint_id}/models",
        json={"model_name": "to-delete", "temperature": 0.5},
    )
    assert model_resp.status_code == 200
    config_id = model_resp.json()["id"]

    # Verify it exists
    async with db.execute(
        "SELECT COUNT(*) as count FROM model_configs WHERE id = ?",
        (config_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["count"] == 1

    # Delete the model config
    delete_resp = await client.delete(f"/api/models/{config_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"ok": True}

    # Verify it's gone from DB
    async with db.execute(
        "SELECT COUNT(*) as count FROM model_configs WHERE id = ?",
        (config_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["count"] == 0


async def test_cannot_create_model_config_for_nonexistent_endpoint(client, db):
    """Test creating model config for non-existent endpoint returns error"""
    resp = await client.post(
        "/api/endpoints/99999/models",
        json={"model_name": "test", "temperature": 0.5},
    )
    # Should return 404 or 400
    assert resp.status_code in (404, 400)


async def test_endpoint_crud_workflow(client, db):
    """Test complete CRUD workflow for endpoints"""
    # 1. Create endpoint
    create_resp = await client.post(
        "/api/endpoints",
        json={"url": "https://workflow.example.com", "api_key": "workflow-key"},
    )
    assert create_resp.status_code == 200
    endpoint_id = create_resp.json()["id"]

    # 2. Verify in list
    list_resp = await client.get("/api/endpoints")
    assert list_resp.status_code == 200
    endpoints = list_resp.json()
    assert any(e["id"] == endpoint_id for e in endpoints)

    # 3. Create model config for endpoint
    model_resp = await client.post(
        f"/api/endpoints/{endpoint_id}/models",
        json={"model_name": "workflow-model", "temperature": 0.6},
    )
    assert model_resp.status_code == 200
    config_id = model_resp.json()["id"]

    # 4. Verify model config in list
    models_resp = await client.get(f"/api/endpoints/{endpoint_id}/models")
    assert models_resp.status_code == 200
    models = models_resp.json()
    assert any(m["id"] == config_id for m in models)

    # 5. Delete model config
    delete_model_resp = await client.delete(f"/api/models/{config_id}")
    assert delete_model_resp.status_code == 200

    # 6. Verify model config deleted
    models_resp2 = await client.get(f"/api/endpoints/{endpoint_id}/models")
    assert models_resp2.status_code == 200
    models2 = models_resp2.json()
    assert not any(m["id"] == config_id for m in models2)

    # 7. Delete endpoint
    delete_endpoint_resp = await client.delete(f"/api/endpoints/{endpoint_id}")
    assert delete_endpoint_resp.status_code == 200

    # 8. Verify endpoint deleted
    list_resp2 = await client.get("/api/endpoints")
    assert list_resp2.status_code == 200
    endpoints2 = list_resp2.json()
    assert not any(e["id"] == endpoint_id for e in endpoints2)
