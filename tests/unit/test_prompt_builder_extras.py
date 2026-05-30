"""Coverage for the prefix-extras kwarg and the attachment source-branch.

Empty-input parity is the load-bearing property: when no workflow yields
a system_prompt or stages an attachment, prefix bytes must match the
shape produced when neither extension exists.
"""

from __future__ import annotations

from backend.prompt_builder import build_prefix, format_message_with_attachments


_BASE_KWARGS = dict(
    system_prompt="You are an assistant.",
    char_persona="A test character.",
    char_scenario="In a test scenario.",
)


def _system_body(prefix: list[dict]) -> str:
    assert prefix[0]["role"] == "system"
    return prefix[0]["content"]


# -- build_prefix(extra_system_blocks=...) parity -------------------------


def test_build_prefix_no_extras_kwarg_matches_unspecified():
    base = build_prefix(**_BASE_KWARGS)
    explicit_none = build_prefix(extra_system_blocks=None, **_BASE_KWARGS)
    explicit_empty = build_prefix(extra_system_blocks=[], **_BASE_KWARGS)
    assert _system_body(base) == _system_body(explicit_none) == _system_body(explicit_empty)


def test_build_prefix_extras_appended_with_blank_line_separator():
    out = build_prefix(extra_system_blocks=["EXTRA_BLOCK"], **_BASE_KWARGS)
    body = _system_body(out)
    assert body.endswith("\n\nEXTRA_BLOCK")


def test_build_prefix_multiple_extras_appended_in_order():
    out = build_prefix(extra_system_blocks=["FIRST", "SECOND"], **_BASE_KWARGS)
    body = _system_body(out)
    first_idx = body.index("FIRST")
    second_idx = body.index("SECOND")
    assert first_idx < second_idx
    # Each extra is on its own block with a blank line before it.
    assert "\n\nFIRST" in body
    assert "\n\nSECOND" in body


def test_build_prefix_extras_appear_after_existing_body():
    out_no_extras = _system_body(build_prefix(**_BASE_KWARGS))
    out_with = _system_body(build_prefix(extra_system_blocks=["TAIL"], **_BASE_KWARGS))
    assert out_with.startswith(out_no_extras)
    assert out_with == out_no_extras + "\n\nTAIL"


# -- format_message_with_attachments source branching ---------------------


def test_format_no_attachments_returns_string_content():
    msg = {"role": "user", "content": "hello"}
    assert format_message_with_attachments(msg, macros=None) == {
        "role": "user",
        "content": "hello",
    }


def test_format_no_attachments_empty_content_returns_empty_string():
    msg = {"role": "user", "content": ""}
    assert format_message_with_attachments(msg, macros=None) == {
        "role": "user",
        "content": "",
    }


def test_format_user_attachment_only_produces_multimodal_parts():
    msg = {
        "role": "user",
        "content": "look",
        "user_attachments": [
            {"mime_type": "image/png", "data_b64": "ZmFrZQ=="},
        ],
        "workflow_attachments": [],
    }
    out = format_message_with_attachments(msg, macros=None)
    assert out["role"] == "user"
    assert isinstance(out["content"], list)
    assert out["content"][0] == {"type": "text", "text": "look"}
    assert out["content"][1]["type"] == "image_url"
    assert out["content"][1]["image_url"]["url"] == "data:image/png;base64,ZmFrZQ=="


def test_format_workflow_root_with_annotation_appends_to_text():
    msg = {
        "role": "assistant",
        "content": "spoken line",
        "user_attachments": [],
        "workflow_attachments": [
            {
                "workflow_id": "tts",
                "parent_attachment_id": None,
                "annotation": "[audio: 4 second clip]",
                "mime_type": "audio/mpeg",
                "data_b64": "QUJDRA==",
            },
        ],
    }
    out = format_message_with_attachments(msg, macros=None)
    # Workflow attachments are NEVER multimodal; bytes don't leak to prefix.
    assert isinstance(out["content"], str)
    assert out["content"] == "spoken line\n\n[audio: 4 second clip]"


def test_format_workflow_root_without_annotation_contributes_nothing():
    msg = {
        "role": "assistant",
        "content": "silent",
        "user_attachments": [],
        "workflow_attachments": [
            {
                "workflow_id": "tts",
                "parent_attachment_id": None,
                "annotation": None,
                "mime_type": "audio/mpeg",
                "data_b64": "QQ==",
            },
        ],
    }
    out = format_message_with_attachments(msg, macros=None)
    assert out == {"role": "assistant", "content": "silent"}


def test_format_workflow_root_with_whitespace_annotation_ignored():
    msg = {
        "role": "assistant",
        "content": "draft",
        "user_attachments": [],
        "workflow_attachments": [
            {
                "workflow_id": "tts",
                "parent_attachment_id": None,
                "annotation": "   \n  ",
                "mime_type": "audio/mpeg",
                "data_b64": "QQ==",
            },
        ],
    }
    out = format_message_with_attachments(msg, macros=None)
    assert out == {"role": "assistant", "content": "draft"}


def test_format_workflow_sibling_variant_ignored_even_with_annotation():
    msg = {
        "role": "assistant",
        "content": "draft",
        "user_attachments": [],
        "workflow_attachments": [
            {
                "workflow_id": "tts",
                "parent_attachment_id": 17,
                "annotation": "should-not-appear",
                "mime_type": "audio/mpeg",
                "data_b64": "QQ==",
            },
        ],
    }
    out = format_message_with_attachments(msg, macros=None)
    assert out == {"role": "assistant", "content": "draft"}


def test_format_mixed_user_image_and_workflow_annotation():
    msg = {
        "role": "user",
        "content": "describe",
        "user_attachments": [
            {"mime_type": "image/png", "data_b64": "WA=="},
        ],
        "workflow_attachments": [
            {
                "workflow_id": "scenebot",
                "parent_attachment_id": None,
                "annotation": "scene tag",
                "mime_type": "image/png",
                "data_b64": "ZZ==",
            },
        ],
    }
    out = format_message_with_attachments(msg, macros=None)
    assert isinstance(out["content"], list)
    assert out["content"][0] == {"type": "text", "text": "describe\n\nscene tag"}
    # Only the user image is included as multimodal; the workflow row stays out.
    image_parts = [p for p in out["content"] if p.get("type") == "image_url"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"] == "data:image/png;base64,WA=="


def test_format_attachment_with_only_user_list_treated_as_user():
    msg = {
        "role": "user",
        "content": "legacy",
        "user_attachments": [
            {"mime_type": "image/png", "data_b64": "WA=="},
        ],
        "workflow_attachments": [],
    }
    out = format_message_with_attachments(msg, macros=None)
    assert isinstance(out["content"], list)
    assert out["content"][1]["type"] == "image_url"


def test_format_workflow_annotation_with_empty_message_text():
    msg = {
        "role": "assistant",
        "content": "",
        "user_attachments": [],
        "workflow_attachments": [
            {
                "workflow_id": "tts",
                "parent_attachment_id": None,
                "annotation": "voice line",
                "mime_type": "audio/mpeg",
                "data_b64": "QQ==",
            },
        ],
    }
    out = format_message_with_attachments(msg, macros=None)
    assert out == {"role": "assistant", "content": "voice line"}
