"""Post-processing fragment gates, prompts, schemas, and exact patch safety."""

from __future__ import annotations

from backend.pipeline.config import (
    _build_writer_tools_blob,
    _split_interactive_fragments,
)
from backend.pipeline.passes.editor import (
    apply_search_replace_patches,
    post_processing_active,
)
from backend.pipeline.passes.editor.prompts import build_post_processing_prompt
from backend.prompting import build_style_injection
from backend.prompting.tool_schemas import (
    EDITOR_SEARCH_REPLACE_TOOL,
    build_direct_scene_tool,
)


def _fragment(fid: str, field_type: str, sort_order: int = 0) -> dict:
    return {
        "id": fid,
        "label": fid,
        "injection_label": fid.replace("_", " ").title(),
        "description": f"Instruction for {fid}",
        "field_type": field_type,
        "required": False,
        "sort_order": sort_order,
    }


def test_fragment_split_has_four_disjoint_groups_and_leaves_decisions_out():
    fragments = [
        _fragment("plot", "string"),
        _fragment("feedback", "feedback"),
        _fragment("note", "direction_note"),
        _fragment("humanize", "post_processing"),
        _fragment("outcome", "decision"),
    ]
    writer, feedback, notes, post_processing = _split_interactive_fragments(fragments)
    assert [[f["id"] for f in group] for group in (writer, feedback, notes, post_processing)] == [
        ["plot"],
        ["feedback"],
        ["note"],
        ["humanize"],
    ]


def test_post_processing_activation_requires_agent_and_fragment():
    fragment = _fragment("humanize", "post_processing")
    assert post_processing_active([fragment], agent_on=True)
    assert not post_processing_active([], agent_on=True)
    assert not post_processing_active([fragment], agent_on=False)


def test_tool_blob_activation_is_independent_of_output_auditor():
    enabled_tools = {"direct_scene": True, "editor_apply_patch": False}
    _build_writer_tools_blob(
        {"enable_agent": True},
        [_fragment("humanize", "post_processing")],
        enabled_tools,
    )
    assert enabled_tools["editor_search_replace"] is True
    assert enabled_tools["editor_apply_patch"] is False


def test_tool_blob_does_not_activate_when_agent_is_off():
    enabled_tools = {"direct_scene": True}
    _build_writer_tools_blob(
        {"enable_agent": False},
        [_fragment("humanize", "post_processing")],
        enabled_tools,
    )
    assert "editor_search_replace" not in enabled_tools


def test_post_processing_never_enters_director_schema_or_scene_direction():
    fragment = _fragment("humanize", "post_processing")
    properties = build_direct_scene_tool([fragment])["function"]["parameters"]["properties"]
    assert "humanize" not in properties
    assert "Rewrite me" not in build_style_injection(
        [],
        interactive_fragments=[fragment],
        extra_fields={"humanize": "Rewrite me"},
    )


def test_prompt_uses_injection_label_as_heading_and_description_as_instruction():
    fragment = _fragment("humanize", "post_processing")
    fragment["injection_label"] = "Humanize Dialogue"
    fragment["description"] = "Change dialogue only."
    prompt = build_post_processing_prompt(fragment)
    assert "## Humanize Dialogue" in prompt
    assert "Change dialogue only." in prompt
    assert "editor_search_replace" in prompt
    assert prompt.startswith("[OOC:") and prompt.endswith("]")


def test_prompts_for_different_fragments_share_everything_before_the_heading():
    first = _fragment("humanize", "post_processing")
    first["injection_label"] = "Humanize Dialogue"
    first["description"] = "Change dialogue only."
    second = _fragment("tighten", "post_processing")
    second["injection_label"] = "Tighten Prose"
    second["description"] = "Cut filler."
    a, b = build_post_processing_prompt(first), build_post_processing_prompt(second)
    shared = a[: a.index("## Humanize Dialogue")]
    assert b.startswith(shared)
    assert "SEARCH-AND-REPLACE RULES:" in shared


def test_search_replace_tool_schema_contract():
    function = EDITOR_SEARCH_REPLACE_TOOL["function"]
    assert function["name"] == "editor_search_replace"
    patches = function["parameters"]["properties"]["patches"]
    assert patches["type"] == "array"
    assert list(patches["items"]["properties"]) == ["search", "replace"]
    assert patches["items"]["required"] == ["search", "replace"]


def test_exact_patches_apply_sequentially_against_evolving_draft():
    assert (
        apply_search_replace_patches(
            "Hello there.",
            [
                {"search": "Hello", "replace": "Hey"},
                {"search": "Hey there.", "replace": "Hey."},
            ],
        )
        == "Hey."
    )


def test_empty_replacement_deletes_unique_span():
    assert apply_search_replace_patches("Keep [aside] this.", [{"search": "[aside] ", "replace": ""}]) == "Keep this."


def test_mixed_invalid_and_valid_patches_preserve_valid_edits():
    patches = [
        None,
        {"search": "", "replace": "x"},
        {"search": "Alpha", "replace": "Alpha"},
        {"search": "missing", "replace": "x"},
        {"search": 3, "replace": "x"},
        {"search": "Beta", "replace": "B"},
    ]
    assert apply_search_replace_patches("Alpha Beta", patches) == "Alpha B"


def test_ambiguous_case_sensitive_or_malformed_patches_are_skipped():
    assert apply_search_replace_patches("same same Same", [{"search": "same", "replace": "x"}]) == "same same Same"
    assert apply_search_replace_patches("aaa", [{"search": "aa", "replace": "x"}]) == "aaa"
    assert apply_search_replace_patches("same Same", [{"search": "Same", "replace": "x"}]) == "same x"
    assert apply_search_replace_patches("draft", {"search": "draft", "replace": "x"}) == "draft"
