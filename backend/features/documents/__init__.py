"""Document-mode continuation, auditing, and prose rewriting."""

from __future__ import annotations

from .audit import DOC_AUDIT_TYPES, audit_document, clean_context, patch_document, trim_incomplete_tail
from .continuation import DOC_CHAT_INSTRUCTION, DocumentContinuer, parse_doc_macros
from .rewrite import scaffold_keeper, split_margins
