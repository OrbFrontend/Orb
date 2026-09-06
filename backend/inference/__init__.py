"""Model execution, provider adaptation, retries, and cache mechanics."""

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
from .retry import RetryPolicy
from .text_completion import has_image_parts

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
]
