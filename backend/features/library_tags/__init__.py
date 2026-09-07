"""Library-wide auto-tagging: a curated vocabulary, applied by the Agent lane.

Self-contained by design. The slice owns its prompt and its schema, imports
nothing from ``pipeline``, and registers nothing in the shared tool catalog —
deleting this package, ``queries/library_tags.py``, ``api/routes/library.py``
and the Manager panel removes the feature whole.

What it does not own is the tags: a run writes ``character_cards.tags``, the
card's one tag list, rather than keeping a parallel set beside it. That is why
tagging a card destroys the tags it was imported with.
"""

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
