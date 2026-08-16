"""Inference layer — LLM transport and prompt/tool assembly.

Depends only on ``core``. Facade re-exports the public surface so callers
write ``from .inference import X``. Private symbols are accessed via the
submodule path directly.
"""

from __future__ import annotations

from .cached_call import CachedBase
from .client import (
    AbortToken,
    LLMClient,
    agent_client_from_settings,
    agent_lane_from_settings,
    client_from_settings,
    parse_tool_calls,
    reasoning_cfg,
    separate_agent_lane_configured,
)
from .endpoint_profiles import (
    ModelProfile,
    honors_forced_tool_choice,
    is_forced_tool_choice,
    note_forced_tool_choice_ignored,
    profile_for,
)
from .errors import LLMCallError, provider_sentence, redact
from .kv_tracker import _KVCacheTracker
from .lorebook import (
    DYNAMIC_SECTION_TITLE,
    compute_constant_lorebook_block,
    compute_depth_lorebook_block,
    is_dynamic,
    select_effective_entries,
)
from .prompt_builder import (
    EDITOR_RENUMBER_NOTICE,
    build_direction_note_prompt,
    build_director_scene_step_prompt,
    build_director_tool_prompt,
    build_editor_prompt,
    build_feedback_prompt,
    build_lorebook_select_prompt,
    build_prefix,
    build_style_injection,
    build_world_change_prompt,
    compute_style_injection_block,
    format_message_with_attachments,
    render_direction_notes_block,
    resolve_mood_fragment_randoms,
)
from .retry import RetryPolicy
from .text_completion import has_image_parts
from .tool_registry import (
    BUILTIN_TOOL_NAMES,
    GIVE_FEEDBACK_CHOICE,
    POST_WRITER_TOOLS,
    PRE_WRITER_TOOLS,
    PROPOSE_WORLD_CHANGES_CHOICE,
    RECORD_DIRECTION_NOTE_CHOICE,
    SELECT_LOREBOOK_CHOICE,
    STANDALONE_TOOLS,
    TOOLS,
    build_direct_scene_tool,
    build_direction_note_tool,
    build_feedback_tool,
    enabled_schemas,
    register_tool,
)

__all__ = [
    # client — LLM transport
    "AbortToken",
    "LLMClient",
    "agent_client_from_settings",
    "agent_lane_from_settings",
    "client_from_settings",
    "parse_tool_calls",
    "reasoning_cfg",
    "separate_agent_lane_configured",
    # retry
    "RetryPolicy",
    # errors — the provider's own words, kept
    "LLMCallError",
    "provider_sentence",
    "redact",
    # endpoint_profiles — provider adapter
    "ModelProfile",
    "honors_forced_tool_choice",
    "is_forced_tool_choice",
    "note_forced_tool_choice_ignored",
    "profile_for",
    # cached_call / kv_tracker
    "CachedBase",
    "_KVCacheTracker",
    # text_completion
    "has_image_parts",
    # lorebook — full surface via .lorebook / features.lorebook facade
    "DYNAMIC_SECTION_TITLE",
    "compute_constant_lorebook_block",
    "compute_depth_lorebook_block",
    "is_dynamic",
    "select_effective_entries",
    # prompt_builder
    "build_director_scene_step_prompt",
    "build_director_tool_prompt",
    "EDITOR_RENUMBER_NOTICE",
    "build_editor_prompt",
    "build_feedback_prompt",
    "build_lorebook_select_prompt",
    "build_direction_note_prompt",
    "build_prefix",
    "build_style_injection",
    "build_world_change_prompt",
    "compute_style_injection_block",
    "format_message_with_attachments",
    "render_direction_notes_block",
    "resolve_mood_fragment_randoms",
    # tool_registry
    "BUILTIN_TOOL_NAMES",
    "GIVE_FEEDBACK_CHOICE",
    "POST_WRITER_TOOLS",
    "PRE_WRITER_TOOLS",
    "RECORD_DIRECTION_NOTE_CHOICE",
    "PROPOSE_WORLD_CHANGES_CHOICE",
    "SELECT_LOREBOOK_CHOICE",
    "STANDALONE_TOOLS",
    "TOOLS",
    "build_direct_scene_tool",
    "build_feedback_tool",
    "build_direction_note_tool",
    "enabled_schemas",
    "register_tool",
]
