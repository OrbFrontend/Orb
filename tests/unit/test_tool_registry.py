"""Unit tests for the tool registry surface: built-in name set, register_tool,
standalone filter, and enabled_schemas ordering."""

from __future__ import annotations

import pytest

from backend.database.seeds import DEFAULT_ENABLED_TOOLS, DEFAULT_SETTINGS
from backend.inference import (
    BUILTIN_TOOL_NAMES,
    POST_WRITER_TOOLS,
    PRE_WRITER_TOOLS,
    STANDALONE_TOOLS,
    TOOLS,
    enabled_schemas,
    register_tool,
)

_TEST_TOOL_NAME = "ut_tool_registry_test"
_TEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": _TEST_TOOL_NAME,
        "description": "test",
        "parameters": {"type": "object", "properties": {}},
    },
}
_TEST_CHOICE = {"type": "function", "function": {"name": _TEST_TOOL_NAME}}


@pytest.fixture
def _restore_registry():
    """Restore TOOLS and STANDALONE_TOOLS after a test mutates them."""
    tools_snapshot = dict(TOOLS)
    standalone_snapshot = set(STANDALONE_TOOLS)
    yield
    TOOLS.clear()
    TOOLS.update(tools_snapshot)
    STANDALONE_TOOLS.clear()
    STANDALONE_TOOLS.update(standalone_snapshot)


class TestBuiltinToolNames:
    def test_matches_tools_keys_at_module_load(self):
        assert BUILTIN_TOOL_NAMES == frozenset(TOOLS.keys())

    def test_is_a_frozenset(self):
        assert isinstance(BUILTIN_TOOL_NAMES, frozenset)


class TestStandaloneToolsBaseline:
    def test_empty_at_module_load(self):
        assert STANDALONE_TOOLS == set()


class TestPipelinePhaseSets:
    """PRE_WRITER_TOOLS and POST_WRITER_TOOLS partition the built-in tools by
    pipeline phase — no overlap, full coverage. give_feedback is a post-writer
    feedback-step tool, not a director tool."""

    def test_phase_sets_are_disjoint(self):
        assert PRE_WRITER_TOOLS.isdisjoint(POST_WRITER_TOOLS)

    def test_phase_sets_partition_builtins(self):
        assert PRE_WRITER_TOOLS | POST_WRITER_TOOLS == BUILTIN_TOOL_NAMES

    def test_give_feedback_is_post_writer(self):
        assert "give_feedback" in POST_WRITER_TOOLS
        assert "give_feedback" not in PRE_WRITER_TOOLS


class TestEnabledToolsHoldsOnlyTools:
    """enabled_tools is a tool-registry switch, not a feature-flag bag. The
    seeded default must only name registered tools — feature flags (length_guard,
    length_guard_enforce, ...) live in their own settings columns."""

    def test_default_enabled_tools_subset_of_registry(self):
        assert set(DEFAULT_ENABLED_TOOLS) <= set(TOOLS)

    def test_length_guard_flags_are_not_in_enabled_tools(self):
        assert "length_guard" not in DEFAULT_ENABLED_TOOLS
        assert "length_guard_enforce" not in DEFAULT_ENABLED_TOOLS

    def test_length_guard_flags_are_top_level_settings(self):
        assert "length_guard_enabled" in DEFAULT_SETTINGS
        assert "length_guard_enforce" in DEFAULT_SETTINGS


class TestEnabledSchemasBaseline:
    def test_none_returns_all_built_in_schemas(self):
        schemas = enabled_schemas(None)
        names = [s["function"]["name"] for s in schemas]
        assert set(names) == BUILTIN_TOOL_NAMES

    def test_none_returns_tools_insertion_order(self):
        schemas = enabled_schemas(None)
        names = [s["function"]["name"] for s in schemas]
        assert names == [
            "direct_scene",
            "editor_apply_patch",
            "editor_rewrite",
            "give_feedback",
            "record_direction_note",
            "select_lorebook",
        ]

    def test_dict_filter_returns_insertion_order_subset(self):
        gated = {
            "editor_rewrite": True,
            "editor_apply_patch": True,
            "direct_scene": False,
        }
        names = [s["function"]["name"] for s in enabled_schemas(gated)]
        assert names == ["editor_apply_patch", "editor_rewrite"]

    def test_empty_dict_returns_nothing(self):
        assert enabled_schemas({}) == []


class TestRegisterTool:
    def test_standalone_true_filters_out_of_schemas(self, _restore_registry):
        register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE, standalone=True)
        assert _TEST_TOOL_NAME in TOOLS
        assert _TEST_TOOL_NAME in STANDALONE_TOOLS
        names = [s["function"]["name"] for s in enabled_schemas(None)]
        assert _TEST_TOOL_NAME not in names

    def test_standalone_false_appears_in_schemas(self, _restore_registry):
        register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE, standalone=False)
        assert _TEST_TOOL_NAME in TOOLS
        assert _TEST_TOOL_NAME not in STANDALONE_TOOLS
        names = [s["function"]["name"] for s in enabled_schemas(None)]
        assert _TEST_TOOL_NAME in names

    def test_standalone_bit_symmetric_on_reregistration(self, _restore_registry):
        register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE, standalone=True)
        assert _TEST_TOOL_NAME in STANDALONE_TOOLS

        register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE, standalone=False)
        assert _TEST_TOOL_NAME not in STANDALONE_TOOLS

        register_tool(_TEST_TOOL_NAME, _TEST_SCHEMA, _TEST_CHOICE, standalone=True)
        assert _TEST_TOOL_NAME in STANDALONE_TOOLS

    def test_registered_tool_lands_at_end_under_insertion_order(self, _restore_registry):
        register_tool(
            "z_late_tool",
            {"type": "function", "function": {"name": "z_late_tool"}},
            {"type": "function", "function": {"name": "z_late_tool"}},
            standalone=False,
        )
        names = [s["function"]["name"] for s in enabled_schemas(None)]
        assert names[-1] == "z_late_tool"
        assert names[:-1] == [
            "direct_scene",
            "editor_apply_patch",
            "editor_rewrite",
            "give_feedback",
            "record_direction_note",
            "select_lorebook",
        ]
