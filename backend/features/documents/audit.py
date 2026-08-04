"""Document-mode Output Auditor — prose-quality scan + one-shot LLM patch.

The doc-mode twin of the chat editor's audit step (``pipeline/passes/editor``):
the same pure-prose scanners run over a generated run ("draft") with the
preceding document text as cross-boundary context, and findings can be fixed by
a single forced search/replace-JSON call that byte-extends the generation
prompt (KV-cache-friendly — see ``patch_document``). Stateless like the rest of
the slice — the client POSTs draft + context after a generation ends (EOS or
Stop), and patches apply only to the draft, never to user prose.

Depends only downward (``analysis`` + ``inference`` + ``core``), mirroring
``continuation.py``; the route (``api/routes/documents.py``) owns the HTTP.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ...analysis import (
    AuditReport,
    apply_patches,
    filter_audit_report_to_text,
    format_report,
    report_to_dict,
    run_audit,
)
from ...analysis.text.text_segmentation import (
    ends_with_sentence_terminator,
    sentence_boundary_ends,
)
from ...core import ChatMessage, extract_hyperparams
from ...inference import TOOLS, LLMClient, parse_tool_calls, reasoning_cfg
from .continuation import _MACRO_RE, build_generation_messages

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

# llama.cpp-style chat-template control tokens (<|im_start|>, <|eot_id|>, …).
# In a Raw-mode document these live on their own scaffold lines; a line
# carrying one is template markup, not prose.
_TEMPLATE_TOKEN_RE = re.compile(r"<\|[^<>]*\|>")

# The patch contract, transport-neutral: text mode never renders the tool
# schema and chat mode forces via response_format (no tools in the prompt), so
# this description is the only shape the model ever sees.
_PATCH_JSON_INSTRUCTION = (
    "The audited text needs fixes. Respond with a JSON object of the form "
    '{"patches": [{"search": "...", "replace": "..."}]} — one search/replace pair per issue. '
    "Each `search` must be copied EXACTLY from the newly generated continuation above — never from "
    "the earlier document text. Rewrite each flagged span boldly to fix its issue while keeping the "
    "surrounding narrative flow, preserving the author's voice, tense, and intent; an empty `replace` "
    "deletes the span. Patch only the continuation."
)


# ── Pure helpers ─────────────────────────────────────────────────────────────


def trim_incomplete_tail(draft: str) -> tuple[str, str]:
    """Split *draft* at the last complete-sentence boundary.

    Returns ``(draft_core, tail_fragment)`` with ``draft_core + tail_fragment
    == draft``, so a patched core can reattach the tail verbatim. Uses the
    analysis layer's sentence-boundary scanner rather than
    a second regex. A draft with no complete sentence returns ``("", draft)``.
    """
    if not draft.strip():
        return "", draft
    if ends_with_sentence_terminator(draft):
        return draft, ""
    last = None
    for end in sentence_boundary_ends(draft):
        last = end
    if last is None:
        return "", draft
    return draft[:last], draft[last:]


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


def build_fix_instruction(report_text: str) -> str:
    """The patch call's trailing instruction: the audit report + the JSON
    patch contract. A pure suffix — the generation framing is the persona."""
    return f"{report_text}\n\n{_PATCH_JSON_INSTRUCTION}"


def build_patch_prompt_raw(base: str, draft_core: str, report_text: str) -> str:
    """Text-transport patch prompt: a byte-extension of the generation prompt.

    *base* is the exact prompt string the generation call sent (the verbatim
    document in raw mode; the re-run ``/apply-template`` render in assisted
    mode). ``base + draft_core`` concatenate with no joiner — the draft
    continued directly from those bytes, so this string extends the exact
    token prefix the generation call left in the server's KV slot; only the
    audit suffix is new work.
    """
    return f"{base}{draft_core}\n\n----\n[Prose audit]\n{build_fix_instruction(report_text)}\n"


def build_patch_messages(context: str, draft_core: str, report_text: str, *, assisted: bool) -> list[ChatMessage]:
    """The patch conversation for the CHAT-transport shapes: the generation
    messages replayed verbatim (byte parity — see ``build_generation_messages``),
    the draft closed as the model's own turn (so ``search`` strings target it),
    and the fix request as a pure suffix. Text mode never uses this — it
    byte-extends the rendered generation prompt instead (see ``patch_document``),
    because a closed assistant turn can render differently from the open
    generation prompt the draft actually followed (e.g. Qwen's injected
    ``<think></think>`` block)."""
    gen_messages, _ = build_generation_messages(context, assisted=assisted, completion_mode="chat")
    return [
        *gen_messages,
        {"role": "assistant", "content": draft_core},
        {"role": "user", "content": build_fix_instruction(report_text)},
    ]


def _extract_patches(resp: dict) -> list:
    """Patches from either forced-call response shape: a ``tool_calls``
    message (message transports — grammar and response_format paths both
    re-synthesize ``forced_tool_message``) or the grammar-constrained JSON
    content of a raw ``/completion``. Unparseable content → ``[]``."""
    patches = [
        p
        for call in parse_tool_calls(resp)
        if call.get("name") == "editor_apply_patch"
        for p in (call.get("arguments") or {}).get("patches", [])
    ]
    if patches:
        return patches
    try:
        data = json.loads(resp.get("content") or "")
    except (json.JSONDecodeError, TypeError):
        return []
    found = data.get("patches") if isinstance(data, dict) else None
    return found if isinstance(found, list) else []


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
    """Re-audit the run, then fix the findings with one forced JSON patch call
    on the writer endpoint. Returns the DocumentPatchResponse payload.

    Patches apply to the trimmed draft core only; a truncated tail fragment is
    reattached verbatim.

    KV-cache contract (docs/architecture/kv-cache.md): the patch prompt is a
    byte-extension of the generation prompt — generation shape replayed
    verbatim (raw string or build_generation_messages), draft appended as the
    model's own turn, audit report as a pure suffix. Forcing is decoding-only
    on every shape (json_schema grammar on the text transport,
    tools_in_prompt=False on chat), so the schema never lands in prompt bytes
    and the generation prefix stays warm for both this call and the next run.
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

    # The patch PROMPT rides the raw, uncapped context (byte parity with the
    # generation prompt is what keeps the KV prefix warm); only the scanners
    # see the cleaned/capped ctx above.
    report_text = format_report(report)
    params = extract_hyperparams(settings, defaults={"temperature": 0.25, "max_tokens": 8192})
    schema = TOOLS["editor_apply_patch"]["schema"]
    if client.completion_mode == "text":
        # Both text shapes byte-extend the generation prompt as a raw
        # continuation: verbatim document (raw) or the re-run /apply-template
        # render (assisted — reconstructing the draft as a closed assistant
        # turn renders different bytes than the open generation prompt the
        # draft followed, e.g. Qwen's injected <think></think> block).
        # json_schema constrains decoding only; prompt bytes untouched.
        if assisted:
            gen_messages, prefill = build_generation_messages(context, assisted=True, completion_mode="text")
            base = await client.render_prompt(gen_messages, prefill=prefill, reasoning=False)
        else:
            base = context
        stream = client.complete_raw(
            build_patch_prompt_raw(base, core, report_text),
            model,
            json_schema=schema["function"]["parameters"],
            **params,
        )
    else:
        messages = build_patch_messages(context, core, report_text, assisted=assisted)
        stream = client.complete(
            messages,
            model,
            tools=[schema],
            tool_choice=TOOLS["editor_apply_patch"]["choice"],
            tools_in_prompt=False,
            **params,
            **reasoning_cfg(False),
        )
    resp: dict = {}
    async for event in stream:
        if event["type"] == "done":
            resp = event["message"]

    patches = _extract_patches(resp)
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
