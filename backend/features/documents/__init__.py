"""Document-mode continuation and auditing."""

from __future__ import annotations

from .audit import (
    DOC_AUDIT_TYPES,
    audit_document,
    clean_context,
    patch_document,
    trim_incomplete_tail,
)
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
    "DOC_AUDIT_TYPES",
    "DOC_CHAT_INSTRUCTION",
    "DocumentContinuer",
    "audit_document",
    "clean_context",
    "parse_doc_macros",
    "patch_document",
    "trim_incomplete_tail",
]
