"""Dependency-free shared kernel."""

from __future__ import annotations

from .domain_types import CastMember, GroupContextMode, TurnCast
from .llm_types import (
    AssistantToolMessage,
    ChatMessage,
    ContentPart,
    WireMessage,
)
from .locks import (
    maintenance_lock,
    wal_anchor_lock,
    workflow_character_state_lock,
    workflow_config_lock,
    workflow_state_lock,
    world_apply_lock,
)
from .macros import Macros, has_inline_macros, resolve_inline, resolve_stored_random
from .reasoning import (
    CALL_BREAK,
    ReasoningChannel,
    joined_delta,
    mark_call_start,
    reasoning_delta_event,
)
from .text_segmentation import (
    ends_with_sentence_terminator,
    find_quote_spans,
    remove_quoted_spans,
    split_sentences,
)
from .utils import (
    agent_lane_max_tokens,
    build_multimodal_content,
    estimate_tokens,
    extract_hyperparams,
    scrub_log,
)

__all__ = [
    # llm_types — wire contracts
    "AssistantToolMessage",
    "ChatMessage",
    "ContentPart",
    "WireMessage",
    "CastMember",
    "GroupContextMode",
    "TurnCast",
    # locks — process-level asyncio locks
    "maintenance_lock",
    "wal_anchor_lock",
    "workflow_character_state_lock",
    "workflow_config_lock",
    "workflow_state_lock",
    "world_apply_lock",
    # macros — string/message transforms
    "Macros",
    "has_inline_macros",
    "resolve_inline",
    "resolve_stored_random",
    # reasoning — per-pass thinking buffers (one blank line between calls)
    "CALL_BREAK",
    "ReasoningChannel",
    "joined_delta",
    "mark_call_start",
    "reasoning_delta_event",
    # text_segmentation — canonical non-workflow prose policy
    "ends_with_sentence_terminator",
    "find_quote_spans",
    "remove_quoted_spans",
    "split_sentences",
    # utils — token/log/multimodal helpers
    "agent_lane_max_tokens",
    "build_multimodal_content",
    "estimate_tokens",
    "extract_hyperparams",
    "scrub_log",
]
