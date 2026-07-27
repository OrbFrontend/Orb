"""Shared kernel — dependency-free leaves imported by every layer above.

This package is the bottom of the one-way dependency order
(``api → {pipeline, features} → workflows → {inference, analysis} → core``).
It imports nothing upward; everything else may import it.

The facade re-exports the kernel surface so callers write ``from .core import X``
regardless of which submodule ``X`` actually lives in. Patch the *canonical*
submodule (e.g. ``backend.core.locks._workflow_state_locks``), never this facade.
"""

from __future__ import annotations

from .llm_types import (
    AssistantToolMessage,
    ChatMessage,
    ContentPart,
    WireMessage,
)
from .locks import (
    conversation_stream_lock,
    extension_lifecycle_lock,
    maintenance_lock,
    stream_idle_lock,
    workflow_character_state_lock,
    workflow_config_lock,
    workflow_state_lock,
)
from .macros import Macros, has_inline_macros, resolve_inline, resolve_stored_random
from .personas import resolve_persona_id
from .tags import MAX_TAG_BYTES, MAX_TAGS_PER_CARD, normalize_tags, tags_equal
from .utils import (
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
    # locks — process-level asyncio locks
    "conversation_stream_lock",
    "extension_lifecycle_lock",
    "maintenance_lock",
    "stream_idle_lock",
    "workflow_character_state_lock",
    "workflow_config_lock",
    "workflow_state_lock",
    # macros — string/message transforms
    "Macros",
    "has_inline_macros",
    "resolve_inline",
    "resolve_persona_id",
    "resolve_stored_random",
    # tags — shared character-tag normalization
    "MAX_TAGS_PER_CARD",
    "MAX_TAG_BYTES",
    "normalize_tags",
    "tags_equal",
    # utils — token/log/multimodal helpers
    "build_multimodal_content",
    "estimate_tokens",
    "extract_hyperparams",
    "scrub_log",
]
