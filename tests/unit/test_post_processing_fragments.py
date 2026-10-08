"""Post-processing fragment gates, prompts, schemas, and exact patch safety."""

from backend.pipeline.config import build_writer_tools_blob, split_interactive_fragments
from backend.pipeline.passes.editor import apply_find_replace_patches, post_processing_active
from backend.pipeline.passes.editor.prompts import build_post_processing_prompt
from backend.prompting import build_style_injection
from backend.prompting.tool_schemas import EDITOR_FIND_REPLACE_TOOL, build_direct_scene_tool


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
        _fragment("note", "state"),
        _fragment("humanize", "post_processing"),
        _fragment("outcome", "decision"),
    ]
    scene, feedback, state, post_processing = split_interactive_fragments(fragments)
    assert [[f["id"] for f in group] for group in (scene, feedback, state, post_processing)] == [
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


def test_tool_blob_does_not_activate_when_agent_is_off():
    _, enabled_tools = build_writer_tools_blob(
        {"enable_agent": False}, [_fragment("humanize", "post_processing")], {"direct_scene": True}
    )
    assert "editor_find_replace" not in enabled_tools


def test_post_processing_never_enters_director_schema_or_scene_direction():
    fragment = _fragment("humanize", "post_processing")
    assert "humanize" not in build_direct_scene_tool([fragment])["function"]["parameters"]["properties"]
    assert "Rewrite me" not in build_style_injection(
        [], interactive_fragments=[fragment], extra_fields={"humanize": "Rewrite me"}
    )


def test_prompt_uses_injection_label_as_heading_and_description_as_instruction():
    fragment = _fragment("humanize", "post_processing")
    fragment["injection_label"] = "Humanize Dialogue"
    fragment["description"] = "Change dialogue only."
    prompt = build_post_processing_prompt(fragment)
    assert "## Humanize Dialogue" in prompt
    assert "Change dialogue only." in prompt
    assert "editor_find_replace" in prompt
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


def test_find_replace_tool_schema_contract():
    function = EDITOR_FIND_REPLACE_TOOL["function"]
    assert function["name"] == "editor_find_replace"
    patches = function["parameters"]["properties"]["patches"]
    assert patches["type"] == "array"
    # Gemma's template renders properties sorted; the declared order must survive that, or replace is written first.
    assert list(patches["items"]["properties"]) == sorted(patches["items"]["properties"]) == ["find", "replace"]
    assert patches["items"]["required"] == ["find", "replace"]


def test_exact_patches_apply_sequentially_against_evolving_draft():
    assert (
        apply_find_replace_patches(
            "Hello there.", [{"find": "Hello", "replace": "Hey"}, {"find": "Hey there.", "replace": "Hey."}]
        )
        == "Hey."
    )


def test_empty_replacement_deletes_unique_span():
    assert apply_find_replace_patches("Keep [aside] this.", [{"find": "[aside] ", "replace": ""}]) == "Keep this."


def test_deleting_a_whole_emphasis_beat_heals_the_seam():
    draft = "Mara waved. *The lamp flickered.* Tobin left."
    assert apply_find_replace_patches(draft, [{"find": "The lamp flickered.", "replace": ""}]) == "Mara waved. Tobin left."


def test_mixed_invalid_and_valid_patches_preserve_valid_edits():
    patches = [
        None,
        {"find": "", "replace": "x"},
        {"find": "Alpha", "replace": "Alpha"},
        {"find": "missing", "replace": "x"},
        {"find": 3, "replace": "x"},
        {"find": "Beta", "replace": "B"},
    ]
    assert apply_find_replace_patches("Alpha Beta", patches) == "Alpha B"


def test_ambiguous_case_sensitive_or_malformed_patches_are_skipped():
    assert apply_find_replace_patches("same same Same", [{"find": "same", "replace": "x"}]) == "same same Same"
    assert apply_find_replace_patches("aaa", [{"find": "aa", "replace": "x"}]) == "aaa"
    assert apply_find_replace_patches("same Same", [{"find": "Same", "replace": "x"}]) == "same x"
    assert apply_find_replace_patches("draft", {"find": "draft", "replace": "x"}) == "draft"
