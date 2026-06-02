"""Integration tests for the workflow config HTTP routes.

PUT /api/workflows/{id}/config persists a workflow's global
config slot as a full replacement; GET returns the effective config
(persisted slot, else the workflow's defaults). An empty {"config": {}}
clears the slot, restoring defaults; a missing or non-dict body is a 422
that leaves the slot untouched. The write is held under
workflow_config_lock so it cannot race the locked read-modify-write that
workflow code uses on the same slot.
"""

from __future__ import annotations

import asyncio

from backend.locks import workflow_config_lock
from backend.workflows import (
    Workflow,
    get_workflow_config,
    register_workflow,
    set_workflow_config,
)

from ._fixtures import _restore_registry  # noqa: F401 -- autouse fixture activated by import


async def test_put_persists_and_echoes_effective_config(client):
    register_workflow(Workflow(id="cfg_a", display_name="A", config_defaults={"style": "noir"}))
    resp = await client.put(
        "/api/workflows/cfg_a/config",
        json={"config": {"style": "bright", "depth": 3}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"config": {"style": "bright", "depth": 3}}
    got = await client.get("/api/workflows/cfg_a/config")
    assert got.status_code == 200
    assert got.json() == {"config": {"style": "bright", "depth": 3}}


async def test_get_unset_slot_returns_defaults(client):
    register_workflow(Workflow(id="cfg_a", display_name="A", config_defaults={"style": "noir"}))
    resp = await client.get("/api/workflows/cfg_a/config")
    assert resp.status_code == 200
    assert resp.json() == {"config": {"style": "noir"}}


async def test_empty_config_clears_slot_and_restores_defaults(client):
    register_workflow(Workflow(id="cfg_a", display_name="A", config_defaults={"style": "noir"}))
    await client.put("/api/workflows/cfg_a/config", json={"config": {"style": "bright"}})
    resp = await client.put("/api/workflows/cfg_a/config", json={"config": {}})
    assert resp.status_code == 200
    assert resp.json() == {"config": {"style": "noir"}}
    got = await client.get("/api/workflows/cfg_a/config")
    assert got.json() == {"config": {"style": "noir"}}


async def test_unregistered_workflow_404(client):
    put = await client.put("/api/workflows/ghost/config", json={"config": {"x": 1}})
    assert put.status_code == 404
    get = await client.get("/api/workflows/ghost/config")
    assert get.status_code == 404


async def test_bad_body_422_leaves_slot_unchanged(client):
    register_workflow(Workflow(id="cfg_a", display_name="A", config_defaults={"style": "noir"}))
    await client.put("/api/workflows/cfg_a/config", json={"config": {"style": "bright"}})
    missing = await client.put("/api/workflows/cfg_a/config", json={})
    assert missing.status_code == 422
    non_dict = await client.put("/api/workflows/cfg_a/config", json={"config": [1, 2]})
    assert non_dict.status_code == 422
    got = await client.get("/api/workflows/cfg_a/config")
    assert got.json() == {"config": {"style": "bright"}}


async def test_per_slot_isolation(client):
    register_workflow(Workflow(id="cfg_a", display_name="A", config_defaults={}))
    register_workflow(Workflow(id="cfg_b", display_name="B", config_defaults={}))
    await client.put("/api/workflows/cfg_a/config", json={"config": {"only": "a"}})
    await client.put("/api/workflows/cfg_b/config", json={"config": {"only": "b"}})
    a = await client.get("/api/workflows/cfg_a/config")
    b = await client.get("/api/workflows/cfg_b/config")
    assert a.json() == {"config": {"only": "a"}}
    assert b.json() == {"config": {"only": "b"}}


async def test_put_write_serializes_under_config_lock(client):
    register_workflow(Workflow(id="cfg_a", display_name="A", config_defaults={}))
    await set_workflow_config("cfg_a", {"start": 1})

    async with workflow_config_lock():
        task = asyncio.create_task(client.put("/api/workflows/cfg_a/config", json={"config": {"start": 2}}))
        # The handler blocks acquiring the same lock; once it has had time to
        # reach the acquire, it must still be pending and must not have written.
        await asyncio.sleep(0.1)
        assert not task.done()
        assert (await get_workflow_config("cfg_a")) == {"start": 1}

    resp = await task
    assert resp.status_code == 200
    assert (await get_workflow_config("cfg_a")) == {"start": 2}
