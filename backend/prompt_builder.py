"""
prompt_builder.py — Functions that assemble system prompts, style injections,
and tool-call prompts for the orchestrator pipeline.
"""
from __future__ import annotations

import re

from .tool_defs import TOOLS


# ── Placeholder replacement

def replace_placeholders(text: str, user_name: str, char_name: str) -> str:
    """Replace {{user}} and {{char}} placeholders with actual names."""
    if not text or not isinstance(text, str):
        return text or ''
    result = text
    if user_name:
        result = re.sub(r'\{\{user\}\}', user_name, result, flags=re.IGNORECASE)
    if char_name:
        result = re.sub(r'\{\{char\}\}', char_name, result, flags=re.IGNORECASE)
    return result


# ── System-prompt prefix

def build_prefix(
    system_prompt: str, char_name: str, char_persona: str, char_scenario: str,
    mes_example: str = "", post_history_instructions: str = "", messages: list[dict] = None,
    user_name: str = "User", user_description: str = "",
) -> list[dict]:
    resolved = {
        key: replace_placeholders(val, user_name, char_name)
        for key, val in {
            "persona": char_persona,
            "scenario": char_scenario,
            "mes_example": mes_example,
            "post_history": post_history_instructions,
            "user_desc": user_description,
        }.items()
    }

    parts = [system_prompt]
    if char_name:
        parts.append(f"\n\n## Character: {char_name}")
    if resolved["persona"]:
        parts.append(f"\n{resolved['persona']}")
    if resolved["scenario"]:
        parts.append(f"\n\n## Scenario\n{resolved['scenario']}")
    if resolved["mes_example"]:
        parts.append(f"\n\n## Example Dialogue\n{resolved['mes_example']}")
    if resolved["post_history"]:
        parts.append(f"\n\n## Additional Instructions\n{resolved['post_history']}")
    if resolved["user_desc"]:
        parts.append(f"\n\n## User: {user_name or 'User'}\n{resolved['user_desc']}")

    processed_messages = [
        {"role": m["role"], "content": replace_placeholders(m["content"], user_name, char_name)}
        for m in (messages or [])
    ]

    return [{"role": "system", "content": "".join(parts)}] + processed_messages


# ── Tool-call prompt

def build_tool_prompt(tool_name: str, user_message: str, active_moods: list[str], fragments: list[dict]) -> str:
    tool = TOOLS.get(tool_name)
    if not tool:
        return ""
    desc = tool["schema"]["function"]["description"]
    parts = [
        "<director_mode>Let's pause to improve the roleplay. Use tool calls to accomplish your task accurately and creatively. Your output will immediately affect how the scenario plays out. Be decisive and avoid overthinking. Think outside the box.",
        f"ONLY call this tool with extreme focus: '{tool_name}' - {desc}</director_mode>"
    ]
    if tool_name == "direct_scene":
        moods = ", ".join(active_moods) or "none"
        frags = "\n".join(f"- [{f['id']}] - use in case: {f['description']}" for f in fragments)
        parts.append(f"Currently active moods: {moods}\n\nAvailable writing moods:\n{frags}")
        parts.append(f"User's latest message (for context, take this into account when directing):\n\"\"\"{user_message}\"\"\"")
    elif tool_name == "rewrite_user_prompt":
        parts.append(f"User's latest message:\n\"\"\"[{user_message}]\"\"\"")
    return "\n\n".join(parts)


# ── Style injection block

def compute_style_injection_block(
    active_moods: list[str],
    prior_moods: list[str],
    fragments: list[dict],
    direct_scene_enabled: bool,
    next_action: str | None = None,
    writing_direction: str | None = None,
    detected_repetitions: list[str] | None = None,
    plot_summary: str | None = None,
    keywords: list[str] | None = None,
) -> str:
    """Compute the style injection block from director-pass outputs.

    When *direct_scene_enabled* is False the active-mood and keyword signals are
    suppressed so the previous turn's director state cannot bleed into the writer.
    Only the unconditional fields (next_action, etc.) are still forwarded in
    that case, though in practice they are also None when direct_scene is off.
    """
    if direct_scene_enabled:
        inj_active_moods = active_moods
        inj_keywords = keywords
    else:
        inj_active_moods = []
        inj_keywords = []

    deactivated = (
        [f for f in fragments if f["id"] in (set(prior_moods) - set(inj_active_moods))]
        if direct_scene_enabled else []
    )
    active = [f for f in fragments if f["id"] in inj_active_moods]

    if not (active or deactivated or next_action or writing_direction
            or detected_repetitions or plot_summary or inj_keywords):
        return ""

    return build_style_injection(
        active, deactivated, next_action, writing_direction,
        detected_repetitions, plot_summary, inj_keywords,
    )


def build_style_injection(
    active: list[dict], deactivated: list[dict] | None = None,
    next_action: str | None = None, writing_direction: str | None = None,
    detected_repetitions: list[str] | None = None,
    plot_summary: str | None = None,
    keywords: list[str] | None = None,
) -> str:
    parts = ["**Scene Direction**"]
    if plot_summary:
        parts.append(f"Plot summary: {plot_summary}")
    if next_action:
        parts.append(f"Next action: {next_action}")
    if writing_direction:
        parts.append(f"Narration: {writing_direction}")
    if detected_repetitions:
        parts.append("Avoid repeating:\n" + "\n".join(f"- {phrase}" for phrase in detected_repetitions))
    if keywords:
        parts.append("Keywords: " + ", ".join(keywords))
    for f in active:
        parts.append(f'Mood [{f["id"]}]: {f["prompt_text"]}')
    for f in (deactivated or []):
        if neg := f.get("negative_prompt", "").strip():
            parts.append(f'Deactivated [{f["id"]}]: {neg}')
    return "\n\n".join(parts)