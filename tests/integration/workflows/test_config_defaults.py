"""Integration tests for the get_workflow_config defaults fallback.

The DB layer returns ``{}`` for an empty slot. The registry wrapper layers
the registered workflow's ``config_defaults`` on top so callers always
see a populated dict whenever the workflow ships defaults. A non-empty
persisted slot shadows defaults entirely; clearing the slot restores
defaults; an unregistered id falls through to ``{}``.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.workflows import (
    Workflow,
    get_workflow_config,
    register_workflow,
    set_workflow_config,
)
from backend.workflows import registry as registry_module
from backend.tool_defs import STANDALONE_TOOLS, TOOLS


@pytest.fixture(autouse=True)
def _restore_registry():
    by_id_snapshot = {k: deepcopy(v) for k, v in registry_module._WORKFLOWS_BY_ID.items()}
    tools_snapshot = dict(TOOLS)
    standalone_snapshot = set(STANDALONE_TOOLS)
    yield
    registry_module._WORKFLOWS_BY_ID.clear()
    registry_module._WORKFLOWS_BY_ID.update(by_id_snapshot)
    TOOLS.clear()
    TOOLS.update(tools_snapshot)
    STANDALONE_TOOLS.clear()
    STANDALONE_TOOLS.update(standalone_snapshot)


async def test_empty_slot_returns_defaults(client):
    register_workflow(Workflow(id="cd_a", display_name="A", config_defaults={"x": 1, "y": "hello"}))
    cfg = await get_workflow_config("cd_a")
    assert cfg == {"x": 1, "y": "hello"}


async def test_persisted_slot_shadows_defaults(client):
    register_workflow(Workflow(id="cd_a", display_name="A", config_defaults={"x": 1}))
    await set_workflow_config("cd_a", {"y": 2})
    cfg = await get_workflow_config("cd_a")
    assert cfg == {"y": 2}


async def test_clearing_slot_restores_defaults(client):
    register_workflow(Workflow(id="cd_a", display_name="A", config_defaults={"x": 1}))
    await set_workflow_config("cd_a", {"y": 2})
    await set_workflow_config("cd_a", {})
    cfg = await get_workflow_config("cd_a")
    assert cfg == {"x": 1}


async def test_unregistered_workflow_returns_empty(client):
    cfg = await get_workflow_config("not_registered")
    assert cfg == {}


async def test_defaults_returned_as_fresh_copy(client):
    register_workflow(Workflow(id="cd_a", display_name="A", config_defaults={"x": 1}))
    cfg1 = await get_workflow_config("cd_a")
    cfg1["x"] = 999
    cfg2 = await get_workflow_config("cd_a")
    assert cfg2 == {"x": 1}
