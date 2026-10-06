import pytest

from tests.integration._llm_mock import _pass_from_tool_choice


def test_none_routes_to_writer():
    assert _pass_from_tool_choice(None) == "writer"


def test_literal_none_string_routes_to_writer():
    assert _pass_from_tool_choice("none") == "writer"


def test_auto_routes_to_editor():
    assert _pass_from_tool_choice("auto") == "editor"


def test_editor_apply_patch_routes_to_editor():
    assert _pass_from_tool_choice({"type": "function", "function": {"name": "editor_apply_patch"}}) == "editor"


def test_editor_rewrite_routes_to_editor():
    assert _pass_from_tool_choice({"type": "function", "function": {"name": "editor_rewrite"}}) == "editor"


def test_editor_search_replace_routes_to_post_processing():
    assert _pass_from_tool_choice({"type": "function", "function": {"name": "editor_search_replace"}}) == "post_processing"


def test_direct_scene_routes_to_director():
    assert _pass_from_tool_choice({"type": "function", "function": {"name": "direct_scene"}}) == "director"


def test_arbitrary_function_name_routes_to_workflow():
    assert _pass_from_tool_choice({"type": "function", "function": {"name": "custom_workflow_tool"}}) == "workflow"


def test_dict_without_function_name_raises():
    # No production pass forces a function without a name; treating this as a
    # silent "director" route would mask a malformed tool_choice.
    with pytest.raises(ValueError):
        _pass_from_tool_choice({"type": "function", "function": {}})


def test_unrecognized_string_raises():
    with pytest.raises(ValueError):
        _pass_from_tool_choice("required")
