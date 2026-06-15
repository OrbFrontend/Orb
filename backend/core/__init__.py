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
    maintenance_lock,
    workflow_character_state_lock,
    workflow_config_lock,
    workflow_state_lock,
)
from .macros import Macros
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
    "maintenance_lock",
    "workflow_character_state_lock",
    "workflow_config_lock",
    "workflow_state_lock",
    # macros — string/message transforms
    "Macros",
    # utils — token/log/multimodal helpers
    "build_multimodal_content",
    "estimate_tokens",
    "extract_hyperparams",
    "scrub_log",
]
