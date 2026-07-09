"""Documents slice — free-form LLM-assisted continuation (Document mode).

A user feature (not a pipeline pass); depends only downward on ``inference`` +
``core``. The route keeps HTTP concerns; prompt/transport policy lives here.
"""

from __future__ import annotations

from .continuation import (
    DOC_ASSIST_CONTINUE,
    DOC_ASSIST_INSTRUCTION,
    DOC_CHAT_INSTRUCTION,
    DocumentContinuer,
    parse_doc_macros,
)

__all__ = [
    "DOC_ASSIST_CONTINUE",
    "DOC_ASSIST_INSTRUCTION",
    "DOC_CHAT_INSTRUCTION",
    "DocumentContinuer",
    "parse_doc_macros",
]
