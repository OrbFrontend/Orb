"""Unit tests for director fragments: tool builder, apply_tool_calls, injection block."""

from __future__ import annotations

import pytest

from backend.tool_defs import build_direct_scene_tool, _DIRECT_SCENE_FIXED_REQUIRED
from backend.passes.director import apply_tool_calls
from backend.prompt_builder import build_style_injection, compute_style_injection_block
from backend.database import SEED_DIRECTOR_FRAGMENTS


# ── build_direct_scene_tool ──────────────────────────────────────────────────


class TestBuildDirectSceneTool:
    def test_returns_function_tool_structure(self):
        tool = build_direct_scene_tool([])
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "direct_scene"
        assert "parameters" in tool["function"]

    def test_moods_always_present(self):
        tool = build_direct_scene_tool([])
        props = tool["function"]["parameters"]["properties"]
        assert "moods" in props
        assert "keywords" not in props  # keywords is a director fragment, not fixed

    def test_string_fragment_added_as_string_property(self):
        frags = [
            {
                "id": "plot_summary",
                "label": "Plot Summary",
                "description": "A summary.",
                "field_type": "string",
                "required": True,
                "injection_label": "Plot summary",
            }
        ]
        tool = build_direct_scene_tool(frags)
        props = tool["function"]["parameters"]["properties"]
        assert "plot_summary" in props
        assert props["plot_summary"]["type"] == "string"
        assert props["plot_summary"]["description"] == "A summary."

    def test_array_fragment_added_as_array_property(self):
        frags = [
            {
                "id": "detected_repetitions",
                "label": "Repetitions",
                "description": "Overused phrases.",
                "field_type": "array",
                "required": False,
                "injection_label": "Avoid repeating",
            }
        ]
        tool = build_direct_scene_tool(frags)
        props = tool["function"]["parameters"]["properties"]
        assert "detected_repetitions" in props
        assert props["detected_repetitions"]["type"] == "array"
        assert props["detected_repetitions"]["items"] == {"type": "string"}

    def test_required_fragment_added_to_required_list(self):
        frags = [
            {
                "id": "next_event",
                "field_type": "string",
                "required": True,
                "description": "Next thing.",
                "injection_label": "Next event",
            }
        ]
        tool = build_direct_scene_tool(frags)
        required = tool["function"]["parameters"]["required"]
        assert "next_event" in required

    def test_optional_fragment_not_in_required_list(self):
        frags = [
            {
                "id": "user_intent",
                "field_type": "string",
                "required": False,
                "description": "User intent.",
                "injection_label": "User intent",
            }
        ]
        tool = build_direct_scene_tool(frags)
        required = tool["function"]["parameters"]["required"]
        assert "user_intent" not in required

    def test_empty_fragments_produces_no_required_fields(self):
        tool = build_direct_scene_tool([])
        assert tool["function"]["parameters"]["required"] == []

    def test_seed_fragments_produce_all_expected_properties(self):
        tool = build_direct_scene_tool(SEED_DIRECTOR_FRAGMENTS)
        props = tool["function"]["parameters"]["properties"]
        for frag in SEED_DIRECTOR_FRAGMENTS:
            assert frag["id"] in props


# ── apply_tool_calls ─────────────────────────────────────────────────────────


class TestApplyToolCalls:
    def test_extracts_moods(self):
        calls = [{"name": "direct_scene", "arguments": {"moods": ["tense", "talkative"], "keywords": []}}]
        moods, refined, extra = apply_tool_calls(calls, [])
        assert moods == ["tense", "talkative"]

    def test_keywords_captured_in_extra_fields(self):
        calls = [{"name": "direct_scene", "arguments": {"moods": [], "keywords": ["sword", "tavern"]}}]
        _, _, extra = apply_tool_calls(calls, [])
        assert extra["keywords"] == ["sword", "tavern"]

    def test_extra_fields_captured(self):
        calls = [
            {
                "name": "direct_scene",
                "arguments": {
                    "moods": [],
                    "keywords": ["sword"],
                    "plot_summary": "They fought.",
                    "next_event": "She runs.",
                },
            }
        ]
        _, _, extra = apply_tool_calls(calls, [])
        assert extra["plot_summary"] == "They fought."
        assert extra["next_event"] == "She runs."
        assert extra["keywords"] == ["sword"]

    def test_only_moods_excluded_from_extra_fields(self):
        calls = [
            {
                "name": "direct_scene",
                "arguments": {"moods": ["tense"], "keywords": ["castle"], "plot_summary": "x"},
            }
        ]
        _, _, extra = apply_tool_calls(calls, [])
        assert "moods" not in extra
        assert "keywords" in extra

    def test_none_and_empty_values_excluded_from_extra_fields(self):
        calls = [
            {
                "name": "direct_scene",
                "arguments": {"moods": [], "keywords": [], "user_intent": None, "writing_direction": ""},
            }
        ]
        _, _, extra = apply_tool_calls(calls, [])
        assert "user_intent" not in extra
        assert "writing_direction" not in extra

    def test_empty_list_excluded_from_extra_fields(self):
        calls = [
            {
                "name": "direct_scene",
                "arguments": {"moods": [], "keywords": [], "detected_repetitions": []},
            }
        ]
        _, _, extra = apply_tool_calls(calls, [])
        assert "detected_repetitions" not in extra

    def test_rewrite_prompt_extracted(self):
        calls = [{"name": "rewrite_user_prompt", "arguments": {"refined_message": "Better message."}}]
        _, refined, _ = apply_tool_calls(calls, [])
        assert refined == "Better message."

    def test_current_moods_used_when_no_direct_scene_call(self):
        calls = [{"name": "rewrite_user_prompt", "arguments": {"refined_message": "x"}}]
        moods, _, _ = apply_tool_calls(calls, ["existing-mood"])
        assert moods == ["existing-mood"]

    def test_empty_tool_calls_returns_current_moods(self):
        moods, refined, extra = apply_tool_calls([], ["foo"])
        assert moods == ["foo"]
        assert refined is None
        assert extra == {}


# ── build_style_injection ────────────────────────────────────────────────────


class TestBuildStyleInjection:
    def _make_frags(self):
        return [
            {"id": "plot_summary", "field_type": "string", "injection_label": "Plot summary", "sort_order": 0},
            {"id": "next_event", "field_type": "string", "injection_label": "Next event", "sort_order": 2},
            {"id": "detected_repetitions", "field_type": "array", "injection_label": "Avoid repeating", "sort_order": 4},
        ]

    def test_header_always_present(self):
        result = build_style_injection([], director_fragments=[], extra_fields={})
        assert "**Scene Direction**" in result

    def test_string_field_rendered_with_label(self):
        frags = self._make_frags()
        extra = {"plot_summary": "They fought hard."}
        result = build_style_injection([], director_fragments=frags, extra_fields=extra)
        assert "Plot summary: They fought hard." in result

    def test_array_field_rendered_as_bullets(self):
        frags = self._make_frags()
        extra = {"detected_repetitions": ["overuse of sighs", "purple prose"]}
        result = build_style_injection([], director_fragments=frags, extra_fields=extra)
        assert "Avoid repeating:" in result
        assert "- overuse of sighs" in result
        assert "- purple prose" in result

    def test_fields_omitted_when_not_in_extra_fields(self):
        frags = self._make_frags()
        result = build_style_injection([], director_fragments=frags, extra_fields={"plot_summary": "x"})
        assert "Next event:" not in result
        assert "Avoid repeating:" not in result

    def test_keywords_rendered_as_array_fragment(self):
        frags = [{"id": "keywords", "field_type": "array", "injection_label": "Keywords", "sort_order": 2}]
        extra = {"keywords": ["sword", "castle"]}
        result = build_style_injection([], director_fragments=frags, extra_fields=extra)
        assert "Keywords:" in result
        assert "- sword" in result
        assert "- castle" in result

    def test_active_mood_rendered(self):
        active = [{"id": "tense", "prompt_text": "Write with tension.", "negative_prompt": ""}]
        result = build_style_injection(active, director_fragments=[], extra_fields={})
        assert "Mood [tense]: Write with tension." in result

    def test_deactivated_mood_with_negative_prompt_rendered(self):
        deactivated = [{"id": "terse", "prompt_text": "Short sentences.", "negative_prompt": "Return to normal length."}]
        result = build_style_injection([], deactivated=deactivated, director_fragments=[], extra_fields={})
        assert "Deactivated [terse]: Return to normal length." in result

    def test_deactivated_mood_without_negative_prompt_skipped(self):
        deactivated = [{"id": "grounded", "prompt_text": "Be realistic.", "negative_prompt": ""}]
        result = build_style_injection([], deactivated=deactivated, director_fragments=[], extra_fields={})
        assert "Deactivated [grounded]" not in result

    def test_sort_order_respected(self):
        frags = [
            {"id": "b_field", "field_type": "string", "injection_label": "B Label", "sort_order": 1},
            {"id": "a_field", "field_type": "string", "injection_label": "A Label", "sort_order": 0},
        ]
        extra = {"a_field": "val_a", "b_field": "val_b"}
        result = build_style_injection([], director_fragments=frags, extra_fields=extra)
        assert result.index("A Label") < result.index("B Label")


# ── compute_style_injection_block ────────────────────────────────────────────


class TestComputeStyleInjectionBlock:
    def _make_director_frags(self):
        return [
            {"id": "plot_summary", "field_type": "string", "injection_label": "Plot summary", "sort_order": 0, "enabled": True},
            {"id": "next_event", "field_type": "string", "injection_label": "Next event", "sort_order": 2, "enabled": True},
        ]

    def _make_mood_frags(self):
        return [{"id": "tense", "prompt_text": "Write with tension.", "negative_prompt": "Relax.", "enabled": True}]

    def test_returns_empty_when_nothing_to_inject(self):
        result = compute_style_injection_block([], [], [], [], True, {})
        assert result == ""

    def test_suppresses_moods_when_direct_scene_disabled(self):
        frags = self._make_mood_frags()
        result = compute_style_injection_block(
            ["tense"], [], frags, [], False, {"plot_summary": "x"}
        )
        assert "Mood [tense]" not in result

    def test_suppresses_extra_fields_when_direct_scene_disabled(self):
        dir_frags = self._make_director_frags()
        result = compute_style_injection_block(
            [], [], [], dir_frags, False, {"plot_summary": "x"}
        )
        assert result == ""

    def test_includes_moods_when_direct_scene_enabled(self):
        frags = self._make_mood_frags()
        result = compute_style_injection_block(
            ["tense"], [], frags, [], True, {"plot_summary": "x"}
        )
        assert "Mood [tense]" in result

    def test_extra_fields_rendered_dynamically(self):
        dir_frags = self._make_director_frags()
        extra = {"plot_summary": "The hero fell.", "next_event": "She escapes."}
        result = compute_style_injection_block([], [], [], dir_frags, True, extra)
        assert "Plot summary: The hero fell." in result
        assert "Next event: She escapes." in result

    def test_keywords_in_extra_fields_rendered_as_array(self):
        dir_frags = [{"id": "keywords", "field_type": "array", "injection_label": "Keywords", "sort_order": 2, "enabled": True}]
        result = compute_style_injection_block([], [], [], dir_frags, True, {"keywords": ["castle", "sword"]})
        assert "Keywords:" in result
        assert "- castle" in result


# ── SEED_DIRECTOR_FRAGMENTS sanity ───────────────────────────────────────────


class TestSeedDirectorFragments:
    STR_FIELDS = ("id", "label", "description", "field_type", "injection_label")

    @pytest.mark.parametrize("frag", SEED_DIRECTOR_FRAGMENTS, ids=lambda f: f.get("id", "?"))
    def test_string_fields_are_str(self, frag):
        for field in self.STR_FIELDS:
            assert isinstance(frag[field], str), f"{frag['id']!r}.{field!r} must be str"

    @pytest.mark.parametrize("frag", SEED_DIRECTOR_FRAGMENTS, ids=lambda f: f.get("id", "?"))
    def test_required_is_bool(self, frag):
        assert isinstance(frag["required"], bool)

    @pytest.mark.parametrize("frag", SEED_DIRECTOR_FRAGMENTS, ids=lambda f: f.get("id", "?"))
    def test_field_type_is_valid(self, frag):
        assert frag["field_type"] in ("string", "array")

    def test_seed_ids_match_original_hardcoded_params(self):
        ids = {f["id"] for f in SEED_DIRECTOR_FRAGMENTS}
        expected = {"plot_summary", "user_intent", "keywords", "next_event", "writing_direction", "detected_repetitions"}
        assert ids == expected
