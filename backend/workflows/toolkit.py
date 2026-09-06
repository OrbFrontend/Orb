"""Stable imports for workflow authors."""

from __future__ import annotations

from typing import Any

from ..analysis import (
    FormatDriftReport,
    build_targets,
    format_numbered_report,
    format_report,
    normalize_to_baseline,
    run_audit,
)
from ..core import (
    Macros,
    workflow_character_state_lock,
    workflow_config_lock,
    workflow_state_lock,
)
from ..core.domain_types import AgentLane, CastMember, TurnCast
from ..database import (
    get_active_lorebook_entries,
    get_character_avatar,
    get_character_card,
    get_conversation,
    get_director_state,
    get_interactive_fragments,
    get_message_by_id,
    get_messages,
    get_mood_fragments,
    get_phrase_bank,
    get_settings,
    get_speaker_names,
    get_user_attachments_for_message,
    get_user_persona,
    get_user_personas,
    get_workflow_attachment_by_id,
    resolve_cast,
    resolve_char_context,
)
from ..inference import (
    LLMClient,
    local_ml,
    parse_tool_calls,
    reasoning_cfg,
    separate_agent_lane_configured,
)
from ..prompting import build_prefix, format_message_with_attachments, macro_identity
from ..prompting.lorebook import compute_constant_lorebook_block
from ..prompting.tool_catalog import STANDALONE_TOOLS, TOOLS, enabled_schemas
from ._forced_call import forced_tool_call
from .attachment_cache import EVICTED_MARKER, insert_workflow_attachment
from .registry import (
    get_workflow_character_state,
    get_workflow_config,
    get_workflow_message_state,
    get_workflow_state,
    overlay_enable_tools,
    set_workflow_character_state,
    set_workflow_config,
    set_workflow_message_state,
    set_workflow_state,
)

__all__ = [
    "CastMember",
    "EVICTED_MARKER",
    "FormatDriftReport",
    "LLMClient",
    "Macros",
    "STANDALONE_TOOLS",
    "TOOLS",
    "TurnCast",
    "build_prefix",
    "enabled_schemas",
    "forced_tool_call",
    "format_message_with_attachments",
    "build_targets",
    "format_numbered_report",
    "format_report",
    "get_character_avatar",
    "get_character_card",
    "get_conversation",
    "get_interactive_fragments",
    "get_director_state",
    "get_message_by_id",
    "get_messages",
    "get_mood_fragments",
    "get_phrase_bank",
    "get_scene_cast",
    "get_settings",
    "get_user_attachments_for_message",
    "get_user_personas",
    "get_user_persona",
    "get_workflow_attachment_by_id",
    "get_workflow_character_state",
    "get_workflow_config",
    "get_workflow_message_state",
    "get_workflow_state",
    "insert_workflow_attachment",
    "local_ml",
    "normalize_to_baseline",
    "overlay_enable_tools",
    "parse_tool_calls",
    "reasoning_cfg",
    "run_audit",
    "build_offturn_prefix",
    "set_workflow_character_state",
    "set_workflow_config",
    "set_workflow_message_state",
    "set_workflow_state",
    "workflow_character_state_lock",
    "workflow_config_lock",
    "workflow_state_lock",
]


async def get_scene_cast(conversation_id: str) -> TurnCast:
    """Return the conversation's resolved cast."""
    conv = await get_conversation(conversation_id)
    return await resolve_cast(conv) if conv is not None else TurnCast(False, ())


async def build_offturn_prefix(
    conversation_id: str,
    history,
    settings,
    *,
    lane: AgentLane = "writer",
) -> list[Any]:
    """Build the character and persona prefix for an off-turn call."""
    if lane not in ("writer", "agent"):
        raise ValueError(f"unknown off-turn model lane {lane!r}")
    conv = await get_conversation(conversation_id)
    if conv is None:
        return []
    card_id = conv.get("character_card_id")
    card = await get_character_card(card_id) if card_id else None
    # A group names no single character: the scene's title is {{char}}, the cast
    # section stands in for the card, and each replayed reply is attributed to
    # the member who wrote it. Resolved through the same reader the turn uses,
    # against the *neutral* base (no speaker) — which is the base the Director
    # runs on in every mode, Classic card swap included.
    turn_cast = await resolve_cast(conv)
    system_prompt, char_persona, mes_example = await resolve_char_context(conv, settings, card=card)
    dual_agent = lane == "agent" and separate_agent_lane_configured(settings)
    if dual_agent:
        system_prompt, _, _ = await resolve_char_context(
            conv,
            settings,
            card=card,
            shared_key="agent_shared_system_prompt",
        )
    persona_id = (
        conv.get("persona_lock_id") or (card.get("persona_lock_id") if card else None) or settings.get("active_persona_id")
    )
    persona = await get_user_persona(persona_id) if persona_id else None
    macro_char, cast_names = macro_identity(conv, turn_cast)
    macros = Macros.from_settings(
        settings, macro_char, persona, seed=conv.get("macro_seed") or conv.get("id", ""), cast=cast_names
    )
    speaker_names = await get_speaker_names(conversation_id) if turn_cast.grouped else {}
    user_description = persona.get("description", "") if persona else settings.get("user_description", "")
    return build_prefix(
        system_prompt,
        char_persona,
        conv.get("character_scenario", ""),
        mes_example,
        "" if settings.get("prevent_prompt_overrides") else conv.get("post_history_instructions", ""),
        history,
        macros,
        user_description,
        constant_lorebook_block=compute_constant_lorebook_block(await get_active_lorebook_entries(), macros),
        cast=turn_cast,
        speaker_names=speaker_names,
    )
