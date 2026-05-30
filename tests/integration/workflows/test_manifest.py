"""Tests for GET /api/workflows."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.workflows import registry as registry_module

from ._fixtures import make_workflow, register_for_test


@pytest.fixture(autouse=True)
def _empty_registry():
    # These tests assert the manifest's exact list contents, so clear the
    # first-party workflows registered at import time and restore them on
    # teardown; each test then controls the whole registry itself.
    snapshot = {k: deepcopy(v) for k, v in registry_module._WORKFLOWS_BY_ID.items()}
    registry_module._WORKFLOWS_BY_ID.clear()
    yield
    registry_module._WORKFLOWS_BY_ID.clear()
    registry_module._WORKFLOWS_BY_ID.update(snapshot)


async def test_empty_registry_returns_empty_list(client):
    resp = await client.get("/api/workflows")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_registered_workflow_appears_with_all_fields(client):
    async def regen(regen_ctx, payload):
        return []

    async def reroll(ctx, params, seed):
        return b""

    wf = make_workflow(
        "scene_cg",
        display_name="Scene CG",
        regenerate=regen,
        reroll_gen=reroll,
        produces_artifacts=True,
        config_schema={"type": "object"},
        config_defaults={"style": "noir"},
    )
    with register_for_test(wf):
        resp = await client.get("/api/workflows")
    assert resp.status_code == 200
    body = resp.json()
    assert body == [
        {
            "id": "scene_cg",
            "display_name": "Scene CG",
            "config_schema": {"type": "object"},
            "config_defaults": {"style": "noir"},
        }
    ]


async def test_manifest_follows_registration_order(client):
    first = make_workflow("first", display_name="First registered")
    second = make_workflow("zzz", display_name="Second registered")
    third = make_workflow("aaa", display_name="Third registered")
    with register_for_test(first), register_for_test(second), register_for_test(third):
        resp = await client.get("/api/workflows")
    ids = [w["id"] for w in resp.json()]
    assert ids == ["first", "zzz", "aaa"]
