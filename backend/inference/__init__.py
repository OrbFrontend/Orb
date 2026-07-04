"""Inference layer — LLM transport and prompt/tool assembly.

Depends only on ``core``. Facade re-exports the public surface so callers
write ``from .inference import X``. Private symbols are accessed via the
submodule path directly.
"""

from __future__ import annotations

from .cached_call import CachedBase
from .client import AbortToken, LLMClient, parse_tool_calls, reasoning_cfg
from .endpoint_profiles import ModelProfile, is_forced_tool_choice, profile_for
from .kv_tracker import _KVCacheTracker
from .prompt_builder import (
    build_direction_note_prompt,
    build_director_scene_step_prompt,
    build_director_tool_prompt,
    build_editor_prompt,
    build_feedback_prompt,
    build_lorebook_select_prompt,
    build_patch_target_prompt,
    build_prefix,
    build_style_injection,
    compute_style_injection_block,
    format_message_with_attachments,
    render_direction_notes_block,
)
from .text_completion import has_image_parts
from .tool_registry import (
    BUILTIN_TOOL_NAMES,
    GIVE_FEEDBACK_CHOICE,
    POST_WRITER_TOOLS,
    PRE_WRITER_TOOLS,
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
    "parse_tool_calls",
    "reasoning_cfg",
    # endpoint_profiles — provider adapter
    "ModelProfile",
    "is_forced_tool_choice",
    "profile_for",
    # cached_call / kv_tracker
    "CachedBase",
    "_KVCacheTracker",
    # text_completion
    "has_image_parts",
    # prompt_builder
    "build_director_scene_step_prompt",
    "build_director_tool_prompt",
    "build_editor_prompt",
    "build_feedback_prompt",
    "build_lorebook_select_prompt",
    "build_direction_note_prompt",
    "build_patch_target_prompt",
    "build_prefix",
    "build_style_injection",
    "compute_style_injection_block",
    "format_message_with_attachments",
    "render_direction_notes_block",
    # tool_registry
    "BUILTIN_TOOL_NAMES",
    "GIVE_FEEDBACK_CHOICE",
    "POST_WRITER_TOOLS",
    "PRE_WRITER_TOOLS",
    "RECORD_DIRECTION_NOTE_CHOICE",
    "SELECT_LOREBOOK_CHOICE",
    "STANDALONE_TOOLS",
    "TOOLS",
    "build_direct_scene_tool",
    "build_feedback_tool",
    "build_direction_note_tool",
    "enabled_schemas",
    "register_tool",
]
