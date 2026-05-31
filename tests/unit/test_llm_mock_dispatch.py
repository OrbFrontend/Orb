from __future__ import annotations

from tests.integration._llm_mock import _pass_from_tool_choice


def test_none_routes_to_writer():
    assert _pass_from_tool_choice(None) == "writer"


def test_literal_none_string_routes_to_writer():
    assert _pass_from_tool_choice("none") == "writer"


def test_auto_routes_to_editor():
    assert _pass_from_tool_choice("auto") == "editor"


def test_editor_apply_patch_routes_to_editor():
    tc = {"type": "function", "function": {"name": "editor_apply_patch"}}
    assert _pass_from_tool_choice(tc) == "editor"


def test_editor_rewrite_routes_to_editor():
    tc = {"type": "function", "function": {"name": "editor_rewrite"}}
    assert _pass_from_tool_choice(tc) == "editor"


def test_direct_scene_routes_to_director():
    tc = {"type": "function", "function": {"name": "direct_scene"}}
    assert _pass_from_tool_choice(tc) == "director"


def test_rewrite_user_prompt_routes_to_director():
    tc = {"type": "function", "function": {"name": "rewrite_user_prompt"}}
    assert _pass_from_tool_choice(tc) == "director"


def test_arbitrary_function_name_routes_to_workflow():
    tc = {"type": "function", "function": {"name": "custom_workflow_tool"}}
    assert _pass_from_tool_choice(tc) == "workflow"


def test_dict_without_function_name_falls_through_to_director():
    assert _pass_from_tool_choice({"type": "function", "function": {}}) == "director"


def test_unrecognized_string_falls_through_to_director():
    assert _pass_from_tool_choice("required") == "director"
