"""Model execution, provider adaptation, retries, and cache mechanics."""

from __future__ import annotations

from .cached_call import CachedBase
from .client import (
    AbortToken,
    LLMClient,
    agent_client_from_settings,
    agent_lane_from_settings,
    client_from_settings,
    lane_template_thinking,
    parse_tool_calls,
    reasoning_cfg,
    replay_reasoning,
    separate_agent_lane_configured,
    strict_tool_arguments,
    until_aborted,
    without_reasoning,
)
from .drafting import (
    BRACES,
    ReplyCutOff,
    assistant_tool_call,
    fence_quote,
    forced_draft,
    forced_turn,
    normalize,
    tool_result,
)
from .endpoint_profiles import (
    ModelProfile,
    honors_forced_tool_choice,
    is_forced_tool_choice,
    note_forced_tool_choice_ignored,
    profile_for,
)
from .errors import EndpointConfigError, LLMCallError, provider_sentence, redact
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
from .kv_tracker import KVCacheTracker
from .retry import RetryPolicy
from .text_completion import has_image_parts
