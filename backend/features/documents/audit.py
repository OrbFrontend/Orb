"""Document-mode Output Auditor — prose-quality scan + one-shot LLM patch.

The doc-mode twin of the chat editor's audit step (``pipeline/passes/editor``):
the same pure-prose scanners run over a generated run ("draft") with the
preceding document text as cross-boundary context, and findings can be fixed by
a single forced ``editor_apply_patch`` call. Stateless like the rest of the
slice — the client POSTs draft + context after a generation ends (EOS or Stop),
and patches apply only to the draft, never to user prose.

Depends only downward (``analysis`` + ``inference`` + ``core``), mirroring
``continuation.py``; the route (``api/routes/documents.py``) owns the HTTP.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Mapping

from ...analysis import (
    AuditReport,
    apply_patches,
    filter_audit_report_to_text,
    format_report,
    report_to_dict,
    run_audit,
)
from ...analysis.text.text_segmentation import SENT_SPLIT
from ...core import extract_hyperparams
from ...inference import TOOLS, LLMClient, parse_tool_calls, reasoning_cfg
from .continuation import _MACRO_RE

if TYPE_CHECKING:
    from ...database.models import PhraseGroup

# The doc-applicable subset of analysis.AUDIT_TYPES: the three chat-context
# scanners (anti_echo, phrase_repetition, structural_repetition) need previous
# assistant messages / the user's last message, which a document doesn't have —
# run_audit auto-skips them when those aren't passed. Single source for both
# the toggle filter here and the panel checkboxes in the frontend.
DOC_AUDIT_TYPES = (
    "banned_phrases",
    "repetitive_openers",
    "repetitive_templates",
    "contrastive_negation",
)

# Semantic cap on the preceding-document context the scanners see (the client
# may send more; the server owns the cap). Keeps the opener/template windows
# spanning the context→draft boundary without scanning a whole novel per run.
DOC_AUDIT_CONTEXT_CHARS = 8000

# Shorter excerpt for the patch prompt — the model only needs enough of the
# preceding document to keep voice/continuity while rewriting flagged spans.
DOC_PATCH_CONTEXT_CHARS = 2000

# A draft that ends on a sentence terminator, tolerating the same trailing
# closing markers (quotes, emphasis, brackets) as the analysis layer's
# SENT_SPLIT, is complete — anchored variant of that boundary definition.
_COMPLETE_END_RE = re.compile(r"[.!?…][\"”’'*_)\]]*\s*$")

# llama.cpp-style chat-template control tokens (<|im_start|>, <|eot_id|>, …).
# In a Raw-mode document these live on their own scaffold lines; a line
# carrying one is template markup, not prose.
_TEMPLATE_TOKEN_RE = re.compile(r"<\|[^<>]*\|>")

DOC_PATCH_SYSTEM = (
    "You are a meticulous line editor for a long-form writing tool. You fix flagged prose issues "
    "with minimal, surgical edits that preserve the author's voice, tense, and intent."
)


# ── Pure helpers ─────────────────────────────────────────────────────────────


def trim_incomplete_tail(draft: str) -> tuple[str, str]:
    """Split *draft* at the last complete-sentence boundary.

    Returns ``(draft_core, tail_fragment)`` with ``draft_core + tail_fragment
    == draft``, so a patched core can reattach the tail verbatim. Uses the
    analysis layer's sentence-boundary definition (``SENT_SPLIT``) rather than
    a second regex. A draft with no complete sentence returns ``("", draft)``.
    """
    if not draft.strip():
        return "", draft
    if _COMPLETE_END_RE.search(draft):
        return draft, ""
    last = None
    for m in SENT_SPLIT.finditer(draft):
        last = m
    if last is None:
        return "", draft
    return draft[: last.end()], draft[last.end() :]


def clean_context(context: str, assisted: bool) -> str:
    """Strip prompt scaffolding from the preceding-document *context*, mode-aware.

    Assisted documents carry ``### SYSTEM/USER/ASSISTANT:`` note lines; Raw
    documents may carry chat-template marker lines (``<|…|>``). Both are
    instructions, not prose — auditing them would flag scaffold "sentences".
    Chat+raw documents are plain prose, so the heuristics are no-ops there.
    Capped to the trailing DOC_AUDIT_CONTEXT_CHARS.
    """
    if assisted:
        lines = [ln for ln in context.split("\n") if not _MACRO_RE.match(ln)]
    else:
        lines = [ln for ln in context.split("\n") if not _TEMPLATE_TOKEN_RE.search(ln)]
    return "\n".join(lines)[-DOC_AUDIT_CONTEXT_CHARS:]


def doc_audit_toggles(toggles: Mapping[str, Any] | None) -> dict:
    """The stored per-scanner map restricted to the doc-applicable subset.

    Missing keys default on, mirroring run_audit's ``_on`` semantics.
    """
    src = toggles or {}
    return {key: bool(src.get(key, True)) for key in DOC_AUDIT_TYPES}


def _audit_sync(
    draft_core: str, context: str, phrase_bank: list[PhraseGroup], toggles: Mapping[str, Any] | None
) -> AuditReport:
    """Audit ``context + draft_core`` (so opener/template windows span the
    boundary), narrowed to draft-only findings — chat-editor semantics. No
    chat context is passed, so the three cross-message scanners never run."""
    text = f"{context}\n\n{draft_core}" if context else draft_core
    report = run_audit(text, phrase_bank, audit_toggles=doc_audit_toggles(toggles))
    return filter_audit_report_to_text(report, draft_core)


def build_patch_messages(draft_core: str, context: str, report_text: str) -> list[dict]:
    """The compact one-shot editor conversation: the draft framed as the
    model's own turn (so ``search`` strings target it), the trailing document
    as the preceding user turn, and the audit report as the fix request."""
    excerpt = context[-DOC_PATCH_CONTEXT_CHARS:]
    doc_turn = (
        f"Here is the latest stretch of the document:\n\n{excerpt}\n\nContinue the document."
        if excerpt
        else "Continue the document."
    )
    fix_turn = (
        f"{report_text}\n\n"
        "Call `editor_apply_patch` with one search/replace pair per issue. Each `search` must be copied "
        "EXACTLY from your continuation above — never from the earlier document text. Rewrite each flagged "
        "span boldly to fix its issue while keeping the surrounding narrative flow; an empty `replace` "
        "deletes the span. Patch only the continuation."
    )
    return [
        {"role": "system", "content": DOC_PATCH_SYSTEM},
        {"role": "user", "content": doc_turn},
        {"role": "assistant", "content": draft_core},
        {"role": "user", "content": fix_turn},
    ]


# ── Route-facing orchestrators ───────────────────────────────────────────────


async def audit_document(
    draft: str,
    context: str,
    phrase_bank: list[PhraseGroup],
    toggles: Mapping[str, Any] | None,
    *,
    assisted: bool,
    truncated: bool,
) -> dict:
    """Scan a generated run. Returns the DocumentAuditResponse payload.

    A truncated run (Stop, or token-budget cutoff) has its dangling partial
    sentence trimmed before auditing — the document itself is untouched; the
    fragment is just never flagged. One partial sentence and nothing else →
    a clean report with the ``no_complete_sentence`` skip marker.
    """
    core, tail = trim_incomplete_tail(draft) if truncated else (draft, "")
    if not core.strip():
        return {
            "report": report_to_dict(AuditReport.clean()),
            "skipped": "no_complete_sentence",
            "tail_excluded": bool(tail.strip()),
        }
    ctx = clean_context(context, assisted)
    # run_audit is CPU-bound; offload it like the editor pass does.
    report = await asyncio.to_thread(_audit_sync, core, ctx, phrase_bank, toggles)
    return {"report": report_to_dict(report), "skipped": None, "tail_excluded": bool(tail.strip())}


async def patch_document(
    client: LLMClient,
    model: str,
    draft: str,
    context: str,
    phrase_bank: list[PhraseGroup],
    toggles: Mapping[str, Any] | None,
    settings: Mapping[str, Any],
    *,
    assisted: bool,
    truncated: bool,
) -> dict:
    """Re-audit the run, then fix the findings with one forced
    ``editor_apply_patch`` call on the writer endpoint. Returns the
    DocumentPatchResponse payload.

    Patches apply to the trimmed draft core only; a truncated tail fragment is
    reattached verbatim. Forced tool calls work on both transports (text mode
    grammar-constrains the JSON), so no doc-mode transport branch is needed.
    """
    core, tail = trim_incomplete_tail(draft) if truncated else (draft, "")
    if not core.strip():
        return {
            "patched_draft": draft,
            "patch_count": 0,
            "errors": [],
            "report_after": report_to_dict(AuditReport.clean()),
            "skipped": "no_complete_sentence",
        }
    ctx = clean_context(context, assisted)
    report = await asyncio.to_thread(_audit_sync, core, ctx, phrase_bank, toggles)
    if report.is_clean:
        return {
            "patched_draft": draft,
            "patch_count": 0,
            "errors": [],
            "report_after": report_to_dict(report),
            "skipped": "clean",
        }

    messages = build_patch_messages(core, ctx, format_report(report))
    params = extract_hyperparams(settings, defaults={"temperature": 0.25, "max_tokens": 8192})
    resp: dict = {}
    async for event in client.complete(
        messages,
        model,
        tools=[TOOLS["editor_apply_patch"]["schema"]],
        tool_choice=TOOLS["editor_apply_patch"]["choice"],
        **params,
        **reasoning_cfg(False),
    ):
        if event["type"] == "done":
            resp = event["message"]

    patches = [
        p
        for call in parse_tool_calls(resp)
        if call.get("name") == "editor_apply_patch"
        for p in (call.get("arguments") or {}).get("patches", [])
    ]
    patched_core, errors = apply_patches(core, patches)
    attempted = len([p for p in patches if isinstance(p, dict) and p.get("search")])
    report_after = await asyncio.to_thread(_audit_sync, patched_core, ctx, phrase_bank, toggles)
    return {
        "patched_draft": patched_core + tail,
        "patch_count": max(0, attempted - len(errors)),
        "errors": errors,
        "report_after": report_to_dict(report_after),
        "skipped": None,
    }
