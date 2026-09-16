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
    assert "local_feature_ready" in toolkit.__all__
    assert "classify_pov" in toolkit.__all__
    assert "classify_pov_tense" in toolkit.__all__
    assert "markup_axes" in toolkit.__all__


def test_toolkit_exposes_format_convention_reading():
    """A plug-in must read one convention for markup repair and voice shaping."""
    for name in ("AxisStyle", "Dialogue", "Narration", "classify_axes", "markup_axes", "narration_only"):
        assert hasattr(toolkit, name)
        assert name in toolkit.__all__


def test_toolkit_exposes_the_span_primitives_a_rewriter_acts_on():
    """Markup repair belongs to format_consistency, but it must act on the same
    span parser the shared classifier reads, not on a private copy of it."""
    for name in (
        "CLOSE_QUOTES",
        "OPEN_QUOTES",
        "TOGGLE_QUOTES",
        "emphasis_inner",
        "extract_block_spans",
        "find_emphasis_spans",
        "find_quote_spans",
        "map_prose",
        "span_role",
        "split_ws",
        "strip_protected_markup",
        "strip_quotes",
    ):
        assert hasattr(toolkit, name), f"{name} not importable from toolkit"
        assert name in toolkit.__all__, f"{name} missing from toolkit.__all__"


def test_toolkit_does_not_own_the_format_consistency_repair_policy():
    """Baseline selection, voting, and the drift report belong to the workflow.
    A toolkit re-export would let another plug-in inherit that policy by accident
    and would import the workflow back into its own API."""
    for name in ("FormatDriftReport", "baseline_axes", "normalize_to_baseline", "stable_label", "vote_axes"):
        assert not hasattr(toolkit, name), f"{name} leaks the workflow's repair policy"
        assert name not in toolkit.__all__


def test_toolkit_is_the_single_plugin_contract_surface():
    for name in _PLUGIN_CONTRACTS:
        assert hasattr(toolkit, name)
        assert name in toolkit.__all__
