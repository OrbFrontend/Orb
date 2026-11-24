"""Shared construction of model-facing message prefixes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..core import CardScripts, ChatMessage, ContentPart, Macros, TurnCast
from ..core.prompt_layout import LayoutBlock, default_layout
from .group_context import render_cast_section
from .layout import PrefixInputs, SectionInputs, join_history, render_prefix


def format_message_with_attachments(
    message: Mapping[str, Any], macros: Macros | None, scripts: CardScripts | None = None
) -> ChatMessage:
    """Convert a message dict to chat format, embedding user attachments.

    Workflow attachment bytes never enter the prefix; annotations from root rows are appended as text.
    """
    role = message["role"]
    raw = message.get("content", "")
    text = macros.resolve_prompt(raw) if macros else raw

    if scripts:
        text = scripts.apply(text, "prompt", role, macros.resolve_prompt if macros else None)

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


def render_history(
    messages: Sequence[Mapping[str, Any]],
    macros: Macros | None,
    *,
    cast: TurnCast | None = None,
    speaker_names: Mapping[str, str] | None = None,
    scripts: CardScripts | None = None,
    speaker_scripts: Mapping[str, CardScripts] | None = None,
) -> list[ChatMessage]:
    """Project stored history into the text and labels the model receives."""
    processed_messages = [
        format_message_with_attachments(
            message,
            macros,
            (speaker_scripts or {}).get(str(message.get("speaker_member_id"))) if cast and cast.grouped else scripts,
        )
        for message in messages
    ]
    if cast and cast.grouped:
        labelled: list[ChatMessage] = []
        names = dict(speaker_names or {})
        names.update({member.member_id: member.name for member in cast.members})
        for original, rendered in zip(messages, processed_messages, strict=True):
            if rendered["role"] != "assistant":
                labelled.append(rendered)
                continue
            label = group_speaker_label(names, original.get("speaker_member_id"))
            content = rendered["content"]
            if isinstance(content, str):
                text = f"{label}: {content}"
                if labelled and labelled[-1]["role"] == "assistant" and isinstance(labelled[-1]["content"], str):
                    labelled[-1] = {"role": "assistant", "content": str(labelled[-1]["content"]) + "\n\n" + text}
                else:
                    labelled.append({"role": "assistant", "content": text})
            else:
                content_parts = list(content)
                if content_parts and content_parts[0]["type"] == "text":
                    first = content_parts[0]
                    content_parts = [{"type": "text", "text": f"{label}: {first['text']}"}, *content_parts[1:]]
                else:
                    content_parts.insert(0, {"type": "text", "text": f"{label}:"})
                labelled.append({"role": "assistant", "content": content_parts})
        processed_messages = labelled
    return processed_messages


def section_inputs(
    description: str,
    char_scenario: str,
    mes_example: str = "",
    post_history_instructions: str = "",
    macros: Macros | None = None,
    user_description: str = "",
    *,
    personality: str = "",
    cast: TurnCast | None = None,
) -> SectionInputs:
    """The card and chat sections layout placeholders stand for, macros resolved; a group's card sections are empty."""
    resolve = macros.resolve_message if macros else (lambda text: text)
    grouped = bool(cast and cast.grouped)
    user_desc = resolve(user_description)
    return SectionInputs(
        description="" if grouped else resolve(description),
        personality="" if grouped else resolve(personality),
        character_heading=not grouped and bool(macros and macros.char),
        scenario=resolve(char_scenario),
        examples="" if grouped else resolve(mes_example),
        instructions=resolve(post_history_instructions),
        persona=user_desc if user_desc.strip() else "",
    )


def prefix_inputs(
    system_prompt: str,
    description: str,
    char_scenario: str,
    mes_example: str = "",
    post_history_instructions: str = "",
    macros: Macros | None = None,
    user_description: str = "",
    *,
    personality: str = "",
    constant_lorebook_block: str = "",
    cast: TurnCast | None = None,
) -> PrefixInputs:
    """Each prefix marker's own text and the sections, macros resolved."""
    resolve = macros.resolve_message if macros else (lambda text: text)
    grouped = bool(cast and cast.grouped)
    return PrefixInputs(
        main_prompt=resolve(system_prompt),
        cast=render_cast_section(cast, macros).lstrip("\n") if cast and grouped else "",
        constant_lore=constant_lorebook_block,
        sections=section_inputs(
            description,
            char_scenario,
            mes_example,
            post_history_instructions,
            macros,
            user_description,
            personality=personality,
            cast=cast,
        ),
    )


def build_prefix(
    system_prompt: str,
    description: str,
    char_scenario: str,
    mes_example: str = "",
    post_history_instructions: str = "",
    messages: Sequence[Mapping[str, Any]] | None = None,
    macros: Macros | None = None,
    user_description: str = "",
    *,
    personality: str = "",
    constant_lorebook_block: str = "",
    cast: TurnCast | None = None,
    speaker_names: Mapping[str, str] | None = None,
    scripts: CardScripts | None = None,
    speaker_scripts: Mapping[str, CardScripts] | None = None,
    layout: Sequence[LayoutBlock] | None = None,
) -> list[ChatMessage]:
    """Build the system message, the layout's pre-history messages, and the rendered history.

    *system_prompt* is the raw Main prompt text; its macros resolve here. *layout* defaults to :func:`default_layout`, the
    built-in order.
    """
    inputs = prefix_inputs(
        system_prompt,
        description,
        char_scenario,
        mes_example,
        post_history_instructions,
        macros,
        user_description,
        personality=personality,
        constant_lorebook_block=constant_lorebook_block,
        cast=cast,
    )
    head = render_prefix(layout if layout is not None else default_layout(), inputs, macros)
    history = render_history(
        messages or [], macros, cast=cast, speaker_names=speaker_names, scripts=scripts, speaker_scripts=speaker_scripts
    )
    return join_history(head, history)
