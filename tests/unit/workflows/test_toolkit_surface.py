"""Pins the workflow locks onto the toolkit's public re-export surface.

Workflow authors import everything from ``backend.workflows.toolkit``,
so the three workflow locks must be reachable there to guard a read-modify-write
on any state tier without importing ``backend.core.locks`` directly. These assertions
fail if a lock is dropped from the re-export, omitted from ``__all__``, or rebound
to something other than the canonical ``backend.core.locks`` object.
"""

from __future__ import annotations

from backend.core import locks
from backend.workflows import toolkit

_LOCK_NAMES = (
    "workflow_state_lock",
    "workflow_character_state_lock",
    "workflow_config_lock",
)

_LOWER_LAYER_INTERNALS = (
    "LLMClient",
    "STANDALONE_TOOLS",
    "TOOLS",
    "build_prefix",
    "enabled_schemas",
    "format_message_with_attachments",
    "local_ml",
    "parse_tool_calls",
    "reasoning_cfg",
)

_PLUGIN_CONTRACTS = (
    "EV_DRAFT_REPLACED",
    "ToolSpec",
    "Workflow",
    "WorkflowEventStream",
    "WorkflowUserFacingError",
)


def test_locks_exported_from_toolkit():
    for name in _LOCK_NAMES:
        assert hasattr(toolkit, name), f"{name} not importable from toolkit"
        assert name in toolkit.__all__, f"{name} missing from toolkit.__all__"


def test_toolkit_locks_are_canonical():
    for name in _LOCK_NAMES:
        assert getattr(toolkit, name) is getattr(locks, name), f"{name} is not the backend.core.locks object"


def test_toolkit_does_not_expose_lower_layer_implementation_objects():
    for name in _LOWER_LAYER_INTERNALS:
        assert not hasattr(toolkit, name), f"{name} leaks through the workflow API"
        assert name not in toolkit.__all__


def test_toolkit_exposes_local_ml_as_narrow_capabilities():
    assert "local_feature_available" in toolkit.__all__
    assert "classify_pov" in toolkit.__all__


def test_toolkit_is_the_single_plugin_contract_surface():
    for name in _PLUGIN_CONTRACTS:
        assert hasattr(toolkit, name)
        assert name in toolkit.__all__
