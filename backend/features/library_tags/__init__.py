"""Public API for library-wide character-card auto-tagging."""

from __future__ import annotations

from .tagger import (
    MAX_TAGS_PER_CARD,
    TAG_TOOL_NAME,
    AutoTagUnavailable,
    build_card_message,
    build_system_prompt,
    build_tag_tool,
    clean_tags,
    tag_card,
)
from .vocabulary import (
    MAX_TAG_LENGTH,
    MAX_VOCABULARY,
    diff_vocabulary,
    normalize_vocabulary,
    vocabulary_hash,
)

__all__ = [
    "MAX_TAGS_PER_CARD",
    "MAX_TAG_LENGTH",
    "MAX_VOCABULARY",
    "TAG_TOOL_NAME",
    "AutoTagUnavailable",
    "build_card_message",
    "build_system_prompt",
    "build_tag_tool",
    "clean_tags",
    "diff_vocabulary",
    "normalize_vocabulary",
    "tag_card",
    "vocabulary_hash",
]
