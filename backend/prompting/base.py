"""Shared construction of model-facing message prefixes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..core import ChatMessage, ContentPart, Macros, TurnCast
from .group_context import render_cast_section


def format_message_with_attachments(message: Mapping[str, Any], macros: Macros | None) -> ChatMessage:
    """Convert a message dict to chat format, embedding user attachments.

    Workflow attachment bytes never enter the prefix; annotations from root
    rows are appended as text.
    """
    role = message["role"]
    raw = message.get("content", "")
    text = macros.resolve_prompt(raw) if macros else raw

    user_atts: list[dict] = list(message.get("user_attachments") or [])
    workflow_annotations: list[str] = []
    for attachment in message.get("workflow_attachments") or []:
        if attachment.get("parent_attachment_id") is not None:
            continue
        annotation = attachment.get("annotation")
        if isinstance(annotation, str) and annotation.strip():
            workflow_annotations.append(annotation)

    text_parts = [text] if text else []
    text_parts.extend(workflow_annotations)
    combined_text = "\n\n".join(text_parts)

    if not user_atts:
        return {"role": role, "content": combined_text}

    parts: list[ContentPart] = []
    if combined_text:
        parts.append({"type": "text", "text": combined_text})
    for attachment in user_atts:
        mime = attachment["mime_type"]
        b64 = attachment["data_b64"]
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return {"role": role, "content": parts}


def group_speaker_label(speaker_names: Mapping[str, str], speaker_member_id: object) -> str:
    """Return the label used for one group assistant history row."""
    if not speaker_member_id:
        return "Summary"
    return speaker_names.get(str(speaker_member_id), "Unknown speaker")


def build_prefix(
    system_prompt: str,
    char_persona: str,
    char_scenario: str,
    mes_example: str = "",
    post_history_instructions: str = "",
    messages: Sequence[Mapping[str, Any]] | None = None,
    macros: Macros | None = None,
    user_description: str = "",
    *,
    constant_lorebook_block: str = "",
    extra_system_blocks: list[str] | None = None,
    cast: TurnCast | None = None,
    speaker_names: Mapping[str, str] | None = None,
) -> list[ChatMessage]:
    """Build the stable system prefix and rendered history messages."""
    resolve = macros.resolve_message if macros else (lambda text: text)
    resolved = {
        key: resolve(value)
        for key, value in {
            "persona": char_persona,
            "scenario": char_scenario,
            "mes_example": mes_example,
            "post_history": post_history_instructions,
            "user_desc": user_description,
        }.items()
    }

    parts = [system_prompt]
    if cast and cast.grouped:
        parts.append(render_cast_section(cast, macros))
    elif macros and macros.char:
        parts.append(f"\n\n## Character: {macros.char}")
    if resolved["persona"] and not (cast and cast.grouped):
        parts.append(f"\n{resolved['persona']}")
    if constant_lorebook_block:
        parts.append(f"\n\n{constant_lorebook_block}")
    if resolved["scenario"]:
        parts.append(f"\n\n## Scenario\n{resolved['scenario']}")
    if resolved["mes_example"] and not (cast and cast.grouped):
        example = resolved["mes_example"]
        if "<START>" in example:
            parts.append(f"\n\n{example.replace('<START>', '## Example Dialogue')}")
        else:
            parts.append(f"\n\n## Example Dialogue\n{example}")
    if resolved["post_history"]:
        parts.append(f"\n\n## Additional Instructions\n{resolved['post_history']}")
    if resolved["user_desc"].strip():
        user_label = macros.user if macros else "User"
        parts.append(f"\n\n## User: {user_label}\n{resolved['user_desc']}")

    for block in extra_system_blocks or []:
        parts.append(f"\n\n{block}")

    original_messages = messages or []
    processed_messages = [format_message_with_attachments(message, macros) for message in original_messages]
    if cast and cast.grouped:
        labelled: list[ChatMessage] = []
        names = dict(speaker_names or {})
        names.update({member.member_id: member.name for member in cast.members})
        for original, rendered in zip(original_messages, processed_messages, strict=True):
            if rendered["role"] != "assistant":
                labelled.append(rendered)
                continue
            label = group_speaker_label(names, original.get("speaker_member_id"))
            content = rendered["content"]
            if isinstance(content, str):
                text = f"{label}: {content}"
                if labelled and labelled[-1]["role"] == "assistant" and isinstance(labelled[-1]["content"], str):
                    labelled[-1] = {
                        "role": "assistant",
                        "content": str(labelled[-1]["content"]) + "\n\n" + text,
                    }
                else:
                    labelled.append({"role": "assistant", "content": text})
            else:
                content_parts = list(content)
                if content_parts and content_parts[0]["type"] == "text":
                    first = content_parts[0]
                    content_parts = [
                        {"type": "text", "text": f"{label}: {first['text']}"},
                        *content_parts[1:],
                    ]
                else:
                    content_parts.insert(0, {"type": "text", "text": f"{label}:"})
                labelled.append({"role": "assistant", "content": content_parts})
        processed_messages = labelled

    system_message: ChatMessage = {"role": "system", "content": "".join(parts)}
    return [system_message] + processed_messages
