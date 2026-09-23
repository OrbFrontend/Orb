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
    replay_reasoning,
    separate_agent_lane_configured,
)
from .drafting import BRACES, ReplyCutOff, forced_draft, forced_turn, normalize
from .endpoint_profiles import (
    ModelProfile,
    honors_forced_tool_choice,
    is_forced_tool_choice,
    note_forced_tool_choice_ignored,
    profile_for,
)
from .errors import LLMCallError, provider_sentence, redact
from .jev import (
    MAX_QUESTION_BYTES,
    MAX_QUESTIONS_PER_REQUEST,
    MAX_REQUEST_BYTES,
    MAX_STATE_BYTES,
    RAW_ANSWER_CACHE,
    CachedAnswer,
    ChoiceAnswer,
    DecisionCancelled,
    DecisionClient,
    DecisionQuestion,
    DecisionResponse,
    DecisionTransportError,
    ScoreAnswer,
    cache_key,
    decisions_url,
)
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
    "replay_reasoning",
    "separate_agent_lane_configured",
    # drafting — forced calls outside the pipeline
    "BRACES",
    "ReplyCutOff",
    "forced_draft",
    "forced_turn",
    "normalize",
    # retry
    "RetryPolicy",
    # errors — the provider's own words, kept
    "LLMCallError",
    "provider_sentence",
    "redact",
    # jev — the decision gateway adapter
    "MAX_QUESTIONS_PER_REQUEST",
    "MAX_QUESTION_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_STATE_BYTES",
    "RAW_ANSWER_CACHE",
    "CachedAnswer",
    "ChoiceAnswer",
    "DecisionCancelled",
    "DecisionClient",
    "DecisionQuestion",
    "DecisionResponse",
    "DecisionTransportError",
    "ScoreAnswer",
    "cache_key",
    "decisions_url",
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
