"""Small shared kernel — closed by default, not a generic helpers package.

This package is the bottom of the one-way dependency order
(``api → {pipeline, features} → workflows → {inference, analysis} → core``).
It imports nothing upward; everything else may import it.

Admission requires more than being pure or having multiple callers. A symbol
must be a canonical host invariant/value contract or process primitive that
cannot retain a legal single-layer owner. It receives already-loaded values and
does no I/O, persistence, configuration lookup, or feature orchestration. See
the "Core admission rule" in ``AGENTS.md``; the module/import inventory is
ratcheted by ``tests/unit/test_import_layering.py``.

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
from .writer_tools import (
    INVALID_ARGUMENTS,
    MAX_WRITER_TOOL_BLOB_BYTES,
    MAX_WRITER_TOOLS_PUBLISHED,
    RESOLVER_UNAVAILABLE,
    TOOL_NOT_AVAILABLE,
    WRITER_TOOL_ERROR_CODES,
    WRITER_TOOL_PREFIX,
    WriterToolError,
    WriterToolInvocation,
    WriterToolKey,
    WriterToolResult,
    WriterToolSpec,
    is_writer_tool_name,
    valid_call_id,
    wire_name,
    writer_tool_error,
    writer_tool_ok,
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
    "resolve_stored_random",
    # personas — effective-persona host policy only
    "resolve_persona_id",
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
    # writer_tools — the Writer-tool ABI shared by workflows/features/pipeline
    "INVALID_ARGUMENTS",
    "MAX_WRITER_TOOLS_PUBLISHED",
    "MAX_WRITER_TOOL_BLOB_BYTES",
    "RESOLVER_UNAVAILABLE",
    "TOOL_NOT_AVAILABLE",
    "WRITER_TOOL_ERROR_CODES",
    "WRITER_TOOL_PREFIX",
    "WriterToolError",
    "WriterToolInvocation",
    "WriterToolKey",
    "WriterToolResult",
    "WriterToolSpec",
    "is_writer_tool_name",
    "valid_call_id",
    "wire_name",
    "writer_tool_error",
    "writer_tool_ok",
]
