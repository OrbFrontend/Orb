"""Stable import surface for workflow authors.

Workflows import from this module rather than reaching directly into
``backend.inference.client``, ``backend.inference.prompt_builder``, etc. The set of
re-exports is the workflow author's API: LLM client, tool-schema
assembly, prompt assembly, macro resolution, read-only DB helpers for
core state, workflow-scoped storage wrappers, the locks guarding
read-modify-write on that storage, the forced-call helper,
the tool-overlay helper, and the editor audit helpers (for workflows
scoring their own outputs against the same audit logic the editor
runs).

Workflows do not import orchestration symbols, the transactional DB
helpers (``add_message``, etc.), director-state
mutators, or pass internals. ``insert_workflow_attachment`` is exported
as the only attachment writer -- the workflow byte cache wraps the raw
row insert with budget + eviction, so all workflow byte writes flow
through one chokepoint.
"""

from __future__ import annotations

from typing import Any

from ..analysis import (
    FormatDriftReport,
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
from ..database import (
    get_character_card,
    get_conversation,
    get_director_state,
    get_interactive_fragments,
    get_message_by_id,
    get_messages,
    get_mood_fragments,
    get_phrase_bank,
    get_user_persona,
    get_user_personas,
    resolve_char_context,
)
from ..inference import (
    STANDALONE_TOOLS,
    TOOLS,
    LLMClient,
    build_prefix,
    enabled_schemas,
    format_message_with_attachments,
    parse_tool_calls,
    reasoning_cfg,
)
from ._forced_call import forced_tool_call
from .attachment_cache import insert_workflow_attachment
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
    "FormatDriftReport",
    "LLMClient",
    "Macros",
    "STANDALONE_TOOLS",
    "TOOLS",
    "build_prefix",
    "enabled_schemas",
    "forced_tool_call",
    "format_message_with_attachments",
    "format_report",
    "get_character_card",
    "get_conversation",
    "get_interactive_fragments",
    "get_director_state",
    "get_message_by_id",
    "get_messages",
    "get_mood_fragments",
    "get_phrase_bank",
    "get_user_personas",
    "get_user_persona",
    "get_workflow_character_state",
    "get_workflow_config",
    "get_workflow_message_state",
    "get_workflow_state",
    "insert_workflow_attachment",
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


async def build_offturn_prefix(
    conversation_id: str,
    history,
    settings,
) -> list[Any]:
    """Rebuild the character/persona prefix for a standalone off-turn call.

    Workflow code cannot import ``pipeline.context`` without violating the
    one-way layer rule. This helper keeps the shared resolution at the workflow
    toolkit boundary and uses the same lower-layer primitives as the pipeline.
    Pre-pipeline workflow blocks are intentionally absent because no turn is in
    flight.
    """
    conv = await get_conversation(conversation_id)
    if conv is None:
        return []
    card_id = conv.get("character_card_id")
    card = await get_character_card(card_id) if card_id else None
    system_prompt, char_persona, mes_example = await resolve_char_context(conv, settings, card=card)
    persona_id = (
        conv.get("persona_lock_id") or (card.get("persona_lock_id") if card else None) or settings.get("active_persona_id")
    )
    persona = await get_user_persona(persona_id) if persona_id else None
    macros = Macros.from_settings(
        settings,
        conv.get("character_name", ""),
        persona,
        seed=conv.get("macro_seed") or conv.get("id", ""),
    )
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
    )
