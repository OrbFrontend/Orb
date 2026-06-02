"""
prompt_builder.py — Functions that assemble system prompts, style injections,
and tool-call prompts for the orchestrator pipeline.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .macros import Macros
from .tool_defs import (
    TOOLS,
    DIRECTOR_PREAMBLE,
    EDITOR_PREAMBLE,
    REASONING_GUIDANCE,
    EDITOR_PATCH_INSTRUCTIONS,
    EDITOR_REWRITE_INSTRUCTIONS,
    EDITOR_BOTH_INSTRUCTIONS,
    STRUCTURAL_REWRITE_INSTRUCTIONS,
)

LOREBOOK_SCAN_DEPTH = 6


def format_message_with_attachments(message: Mapping[str, Any], macros: Macros | None) -> dict:
    """Convert a message dict with optional attachments to OpenAI vision format.

    Two attachment lists travel on the message dict:
      - 'user_attachments': bytes embed as multimodal image_url parts on the
        message content.
      - 'workflow_attachments': bytes never enter the prefix. The 'annotation'
        column on a root row (parent_attachment_id IS NULL) is appended to the
        message text with a blank-line separator. Sibling variants and rows
        with empty or whitespace-only annotations contribute nothing.

    Returns a dict with 'role' and 'content' (string or list of parts).
    """
    role = message["role"]
    raw = message.get("content", "")
    text = macros.resolve_prompt(raw) if macros else raw

    user_atts: list[dict] = list(message.get("user_attachments") or [])
    workflow_annotations: list[str] = []
    for att in message.get("workflow_attachments") or []:
        if att.get("parent_attachment_id") is not None:
            continue
        annot = att.get("annotation")
        if isinstance(annot, str) and annot.strip():
            workflow_annotations.append(annot)

    text_parts = [text] if text else []
    text_parts.extend(workflow_annotations)
    combined_text = "\n\n".join(text_parts)

    if not user_atts:
        return {"role": role, "content": combined_text}

    parts: list[dict] = []
    if combined_text:
        parts.append({"type": "text", "text": combined_text})
    for att in user_atts:
        mime = att["mime_type"]
        b64 = att["data_b64"]
        url = f"data:{mime};base64,{b64}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": role, "content": parts}


# ── System-prompt prefix


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
    extra_system_blocks: list[str] | None = None,
) -> list[dict]:
    resolve = macros.resolve_message if macros else (lambda t: t)
    resolved = {
        key: resolve(val)
        for key, val in {
            "persona": char_persona,
            "scenario": char_scenario,
            "mes_example": mes_example,
            "post_history": post_history_instructions,
            "user_desc": user_description,
        }.items()
    }

    parts = [system_prompt]
    if macros and macros.char:
        parts.append(f"\n\n## Character: {macros.char}")
    if resolved["persona"]:
        parts.append(f"\n{resolved['persona']}")
    if resolved["scenario"]:
        parts.append(f"\n\n## Scenario\n{resolved['scenario']}")
    if resolved["mes_example"]:
        mes = resolved["mes_example"]
        if "<START>" in mes:
            processed_example = mes.replace("<START>", "## Example Dialogue")
            parts.append(f"\n\n{processed_example}")
        else:
            parts.append(f"\n\n## Example Dialogue\n{mes}")
    if resolved["post_history"]:
        parts.append(f"\n\n## Additional Instructions\n{resolved['post_history']}")
    if resolved["user_desc"]:
        user_label = macros.user if macros else "User"
        parts.append(f"\n\n## User: {user_label}\n{resolved['user_desc']}")

    if extra_system_blocks:
        for block in extra_system_blocks:
            parts.append(f"\n\n{block}")

    processed_messages = [format_message_with_attachments(m, macros) for m in (messages or [])]

    return [{"role": "system", "content": "".join(parts)}] + processed_messages


# ── Tool-call prompt


def build_director_tool_prompt(
    tool_name: str,
    user_message: str,
    active_moods: list[str],
    mood_fragments: Sequence[Mapping[str, Any]],
    reasoning_on: bool = False,
    director_fragments: Sequence[Mapping[str, Any]] | None = None,
    progressive_state: dict | None = None,
    tool_schema: dict | None = None,
) -> str:
    tool = TOOLS.get(tool_name)
    if not tool:
        return ""
    schema = tool_schema if tool_schema is not None else tool["schema"]
    desc = schema["function"]["description"]
    params = schema["function"]["parameters"].get("properties", {})
    param_order = ", ".join(params.keys()) if params else "N/A"
    preamble = DIRECTOR_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [
        preamble,
        f"Call ONLY this tool, ensuring parameters follow the schema order: {tool_name} - {desc}\nParameter order: ({param_order})",
    ]
    if tool_name == "direct_scene":
        moods = ", ".join(active_moods) or "none"
        frags = "\n".join(f"* [{f['id']}] - use in case: {f['description']}" for f in mood_fragments)
        parts.append(f"Previously active moods: {moods}\n\nAvailable writing moods:\n{frags}")
        progressive_lines = [
            f"* [{df['id']}] ({df['description']}): {(progressive_state or {}).get(df['id'])}"
            for df in (director_fragments or [])
            if df.get("field_type") == "progressive" and (progressive_state or {}).get(df["id"])
        ]
        if progressive_lines:
            parts.append("Previous progressive fields - dynamically update these:\n" + "\n".join(progressive_lines))
        parts.append(f'User\'s next message (for context, take this into account when directing):\n"""{user_message}"""')
    elif tool_name == "rewrite_user_prompt":
        parts.append(f'User\'s message:\n"""[{user_message}]"""')
    return "\n\n".join(parts)


def build_editor_prompt(
    has_audit_issues: bool,
    report_text: str,
    length_guard_triggered: bool,
    length_guard_instruction: str,
    structural_rewrite: bool = False,
    reasoning_on: bool = False,
) -> str:
    preamble = EDITOR_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]
    rewrite_triggered = length_guard_triggered or structural_rewrite

    if rewrite_triggered:
        parts.append(EDITOR_REWRITE_INSTRUCTIONS)
        if has_audit_issues:
            parts.append(report_text)
        if structural_rewrite:
            parts.append(STRUCTURAL_REWRITE_INSTRUCTIONS)
        if length_guard_triggered:
            parts.append(length_guard_instruction)
        if has_audit_issues and length_guard_triggered:
            parts.append(EDITOR_BOTH_INSTRUCTIONS)
    elif has_audit_issues:
        parts.append(EDITOR_PATCH_INSTRUCTIONS)
        parts.append(report_text)

    return "\n\n".join(parts)


# ── Style injection block


def compute_style_injection_block(
    active_moods: list[str],
    prior_moods: list[str],
    mood_fragments: Sequence[Mapping[str, Any]],
    director_fragments: Sequence[Mapping[str, Any]],
    direct_scene_enabled: bool,
    extra_fields: dict | None = None,
    prior_progressive_state: dict | None = None,
) -> str:
    """Compute the style injection block from director-pass outputs.

    When *direct_scene_enabled* is False, mood signals are suppressed so the
    previous turn's director state cannot bleed into the writer. extra_fields
    are also cleared since all fields (including keywords) now come fresh from
    the current director pass and are empty when it did not run.
    """
    if extra_fields is None:
        extra_fields = {}

    if direct_scene_enabled:
        inj_active_moods = active_moods
        inj_extra = extra_fields
    else:
        inj_active_moods = []
        inj_extra = {}

    deactivated = (
        [f for f in mood_fragments if f["id"] in (set(prior_moods) - set(inj_active_moods))]
        if direct_scene_enabled and inj_active_moods
        else []
    )
    active = [f for f in mood_fragments if f["id"] in inj_active_moods]

    if not (active or deactivated or inj_extra):
        return ""

    return build_style_injection(active, deactivated, director_fragments, inj_extra, prior_progressive_state)


def build_style_injection(
    active: Sequence[Mapping[str, Any]],
    deactivated: Sequence[Mapping[str, Any]] | None = None,
    director_fragments: Sequence[Mapping[str, Any]] | None = None,
    extra_fields: dict | None = None,
    prior_progressive_state: dict | None = None,
) -> str:
    """Render the Scene Direction injection block for the writer pass.

    Director fragment values are rendered in sort_order, each using the
    fragment's injection_label.  Arrays are rendered as bullet lists.
    """
    parts = ["**Scene Direction**"]

    for df in sorted(director_fragments or [], key=lambda x: x.get("sort_order", 0)):
        val = (extra_fields or {}).get(df["id"])
        if not val:
            continue
        label = df["injection_label"]
        if df["field_type"] == "array" and isinstance(val, list):
            parts.append(label + ":\n" + "\n".join(f"- {item}" for item in val))
        elif df["field_type"] == "progressive":
            old_val = (prior_progressive_state or {}).get(df["id"])
            transition = f"{old_val} -> {val}" if old_val and old_val != val else str(val)
            parts.append(f"{label} ({df['description']}): {transition}")
        else:
            parts.append(f"{label}: {val}")

    for f in active:
        parts.append(f["prompt_text"])
    for f in deactivated or []:
        if neg := f.get("negative_prompt", "").strip():
            parts.append(neg)

    return "\n\n".join(parts)


# ── Lorebook injection block


def compute_lorebook_injection_block(
    messages: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
) -> str:
    """Compute the lorebook injection block from active entries.

    Constant entries are always included. Other entries are included when
    one of their keywords appears in the 6 most recent messages (any role).

    Entries are sorted by priority DESC. Returns empty string if no matches.
    """
    if not entries:
        return ""

    scan_parts = [m.get("content") or "" for m in messages[-LOREBOOK_SCAN_DEPTH:] if m.get("content")]
    scan_text = " ".join(scan_parts)
    matched = []

    for entry in entries:
        if entry.get("constant"):
            matched.append(entry)
            continue

        keywords = entry.get("keywords", [])
        if not keywords or not scan_text:
            continue

        case_insensitive = entry.get("case_insensitive", True)
        text = scan_text.lower() if case_insensitive else scan_text

        found = False
        for kw in keywords:
            kw_text = kw.lower() if case_insensitive else kw
            if kw_text in text:
                found = True
                break

        if found:
            matched.append(entry)

    if not matched:
        return ""

    matched.sort(key=lambda e: e.get("priority", 100), reverse=True)

    resolve = macros.resolve_message if macros else (lambda t: t)
    parts = ["**Lorebook**"]
    for entry in matched:
        name = resolve(entry.get("name", ""))
        content = resolve(entry.get("content", ""))
        if name and content:
            parts.append(f"{name}: {content}")
        elif content:
            parts.append(content)

    return "\n\n".join(parts)
