"""Inference layer — LLM transport + prompt/tool assembly (ports & adapters).

Depends only on ``core``. Sits below ``workflows`` and ``pipeline`` in the
one-way dependency order. Internal near-flat cluster: ``client`` → ``endpoint_profiles``,
``cached_call`` → ``kv_tracker``, ``prompt_builder`` → ``tool_registry`` + ``core``.

The facade re-exports the public surface so callers write ``from .inference import X``.
Private submodule symbols (``_serialize_messages``, ``_is_tool_choice_unsupported``,
…) are reached via the canonical submodule path, not this facade.
"""

from __future__ import annotations

from .cached_call import CachedBase
from .client import AbortToken, LLMClient, parse_tool_calls, reasoning_cfg
from .endpoint_profiles import ModelProfile, is_forced_tool_choice, profile_for
from .kv_tracker import _KVCacheTracker
from .prompt_builder import (
    build_director_tool_prompt,
    build_editor_prompt,
    build_feedback_prompt,
    build_lorebook_catalog,
    build_prefix,
    build_style_injection,
    compute_agentic_lorebook_block,
    compute_lorebook_injection_block,
    compute_style_injection_block,
    format_message_with_attachments,
    render_lorebook_block,
)
from .tool_registry import (
    BUILTIN_TOOL_NAMES,
    GIVE_FEEDBACK_CHOICE,
    POST_WRITER_TOOLS,
    PRE_WRITER_TOOLS,
    STANDALONE_TOOLS,
    TOOLS,
    build_direct_scene_tool,
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
    # prompt_builder
    "build_director_tool_prompt",
    "build_editor_prompt",
    "build_feedback_prompt",
    "build_lorebook_catalog",
    "build_prefix",
    "build_style_injection",
    "compute_agentic_lorebook_block",
    "compute_lorebook_injection_block",
    "compute_style_injection_block",
    "format_message_with_attachments",
    "render_lorebook_block",
    # tool_registry
    "BUILTIN_TOOL_NAMES",
    "GIVE_FEEDBACK_CHOICE",
    "POST_WRITER_TOOLS",
    "PRE_WRITER_TOOLS",
    "STANDALONE_TOOLS",
    "TOOLS",
    "build_direct_scene_tool",
    "build_feedback_tool",
    "enabled_schemas",
    "register_tool",
]
