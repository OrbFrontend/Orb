"""Ordered tool catalog contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from backend.database.seeds import DEFAULT_ENABLED_TOOLS
from backend.inference import CachedBase
from backend.pipeline.tools import DIRECTOR_LOOP_TOOL_NAMES
from backend.prompting.tool_catalog import (
    BUILTIN_TOOL_NAMES,
    BUILTIN_TOOL_ORDER,
    STANDALONE_TOOLS,
    TOOLS,
    enabled_schemas,
    register_tool,
    require_tool,
    restore_catalog,
    snapshot_catalog,
)

_TEST_TOOL_NAME = "ut_tool_catalog_test"
_TEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": _TEST_TOOL_NAME,
        "description": "test",
        "parameters": {"type": "object", "properties": {}},
    },
}
_TEST_CHOICE = {"type": "function", "function": {"name": _TEST_TOOL_NAME}}
_BUILTIN_BLOB_LENGTH = 4808
_BUILTIN_BLOB_SHA256 = "671820cf9eff1beefc6707f3f50c2bff646aa167212198d2fd1ab05d5efb7faf"


def _tool_blob(tools: list[dict]) -> str:
    return json.dumps(tools, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture
def _restore_registry():
    snapshot = snapshot_catalog()
    yield
    restore_catalog(snapshot)


def test_builtin_order_is_explicit_and_complete():
    assert BUILTIN_TOOL_ORDER == (
        "direct_scene",
        "editor_apply_patch",
        "editor_rewrite",
        "give_feedback",
        "record_direction_note",
        "select_lorebook",
        "propose_world_changes",
    )
    assert BUILTIN_TOOL_NAMES == frozenset(BUILTIN_TOOL_ORDER)
    assert tuple(TOOLS)[: len(BUILTIN_TOOL_ORDER)] == BUILTIN_TOOL_ORDER


def test_director_loop_membership_is_pipeline_owned():
    assert DIRECTOR_LOOP_TOOL_NAMES == frozenset({"direct_scene"})
    assert DIRECTOR_LOOP_TOOL_NAMES <= BUILTIN_TOOL_NAMES


def test_default_enabled_tools_subset_of_catalog():
    assert set(DEFAULT_ENABLED_TOOLS) <= set(TOOLS)


def test_enabled_schemas_preserves_builtin_order():
    names = [schema["function"]["name"] for schema in enabled_schemas(None)]
    assert names == list(BUILTIN_TOOL_ORDER)


def test_complete_builtin_blob_is_byte_stable():
    blob = _tool_blob(enabled_schemas(None))
    assert len(blob) == _BUILTIN_BLOB_LENGTH
    assert hashlib.sha256(blob.encode()).hexdigest() == _BUILTIN_BLOB_SHA256


async def test_complete_builtin_blob_survives_cached_base_boundary():
    captured: list[str] = []

    class _CapturingClient:
        async def complete(self, **kwargs: Any) -> AsyncIterator[dict]:
            captured.append(_tool_blob(kwargs["tools"]))
            yield {"type": "done", "message": {"role": "assistant", "content": ""}}

    base = CachedBase(
        prefix=({"role": "system", "content": "system"},),
        tools=tuple(enabled_schemas(None)),
        model="model",
    )
    async for _ in base.complete(_CapturingClient(), label="writer", trailing=[]):
        pass

    assert len(captured[0]) == _BUILTIN_BLOB_LENGTH
    assert hashlib.sha256(captured[0].encode()).hexdigest() == _BUILTIN_BLOB_SHA256


def test_enabled_schemas_filters_without_caller_order():
    gated = {
        "editor_rewrite": True,
        "editor_apply_patch": True,
        "direct_scene": False,
    }
    names = [schema["function"]["name"] for schema in enabled_schemas(gated)]
    assert names == ["editor_apply_patch", "editor_rewrite"]
    assert enabled_schemas({}) == []


def test_compatibility_views_are_read_only():
    with pytest.raises(TypeError):
        TOOLS["bad"] = {}  # type: ignore[index]
    with pytest.raises(AttributeError):
        STANDALONE_TOOLS.add("bad")  # type: ignore[attr-defined]

    exposed = TOOLS["direct_scene"]
    exposed["schema"]["function"]["name"] = "bad"
    assert require_tool("direct_scene")["schema"]["function"]["name"] == "direct_scene"

    schemas = enabled_schemas({"direct_scene": True})
    schemas[0]["function"]["name"] = "also_bad"
    assert require_tool("direct_scene")["schema"]["function"]["name"] == "direct_scene"


def test_standalone_registration_is_filtered(_restore_registry):
    register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE, standalone=True)
    assert _TEST_TOOL_NAME in TOOLS
    assert _TEST_TOOL_NAME in STANDALONE_TOOLS
    assert _TEST_TOOL_NAME not in [schema["function"]["name"] for schema in enabled_schemas(None)]


def test_non_standalone_registration_appends(_restore_registry):
    before = [schema["function"]["name"] for schema in enabled_schemas(None)]
    register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE)
    assert [schema["function"]["name"] for schema in enabled_schemas(None)] == [
        *before,
        _TEST_TOOL_NAME,
    ]


def test_reregistration_preserves_position_and_toggles_standalone(_restore_registry):
    register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE, standalone=True)
    position = tuple(TOOLS).index(_TEST_TOOL_NAME)
    register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE, standalone=False)
    assert tuple(TOOLS).index(_TEST_TOOL_NAME) == position
    assert _TEST_TOOL_NAME not in STANDALONE_TOOLS
    register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE, standalone=True)
    assert tuple(TOOLS).index(_TEST_TOOL_NAME) == position
    assert _TEST_TOOL_NAME in STANDALONE_TOOLS
