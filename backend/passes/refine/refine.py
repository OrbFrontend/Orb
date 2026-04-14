"""
refine.py — Refinement pass: audit filtering, text patching, and the
ReAct-style LLM loop that fixes audit issues and/or enforces length guards.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import AsyncIterator

from .audit import run_audit, format_report, AuditReport
from .slop_detector import FlaggedSentence, DetectionResult
from .opening_monotony import FlaggedOpener, MonotonyResult
from .template_repetition import FlaggedTemplate, TemplateResult
from ...llm_client import LLMClient, parse_tool_calls, reasoning_cfg
from ...tool_defs import (
    TOOLS, REFINE_APPLY_PATCH_TOOL, REFINE_REWRITE_TOOL,
    REFINE_PREAMBLE, REFINE_AUDIT_INSTRUCTIONS,
    LENGTH_GUARD_INSTRUCTIONS,
    MAX_REFINE_ITERATIONS, enabled_schemas, reasoning_config_for_schemas,
)

logger = logging.getLogger(__name__)


# ── Audit-report filtering ────────────────────────────────────────────────────

def _split_target_sentences(target_text: str) -> set[str]:
    """Split *target_text* into a sentence set using the same heuristic as the detectors."""
    parts = re.split(r'(?<=[.!?"""\'])\s+', target_text.strip())
    return {s.strip() for s in parts if s.strip()}


def _filter_flagged_items(items, sentences: set[str], total: int, *, cls, label_field: str):
    """Shared helper: filter a list of FlaggedOpener or FlaggedTemplate to
    only include sentences present in *sentences*.

    Returns a list of *cls* instances (with adjusted count / fraction).
    """
    filtered = []
    for item in items:
        kept = [s for s in item.sentences if s in sentences]
        if kept:
            filtered.append(cls(
                **{label_field: getattr(item, label_field)},
                count=len(kept),
                fraction=len(kept) / total if total > 0 else 0.0,
                sentences=kept,
            ))
    return filtered


def filter_audit_report_to_text(report: AuditReport, target_text: str) -> AuditReport:
    """Narrow an audit report to only flag sentences that appear in *target_text*.

    Used when the audit ran on concatenated text (current + previous messages)
    but we only want to surface issues in the latest message.
    """
    target_sents = _split_target_sentences(target_text)

    # Cliché results
    filtered_fs = [fs for fs in report.cliche_result.flagged_sentences if fs.sentence in target_sents]
    filtered_cliche = DetectionResult(
        flagged_sentences=filtered_fs,
        unique_cliches=report.cliche_result.unique_cliches,
        total_sentences=report.cliche_result.total_sentences,
        flagged_count=len(filtered_fs),
    )

    # Opener results
    filtered_openers = _filter_flagged_items(
        report.monotony_result.flagged_openers, target_sents,
        report.monotony_result.total_sentences,
        cls=FlaggedOpener, label_field="opener",
    )
    filtered_monotony = MonotonyResult(
        flagged_openers=filtered_openers,
        all_openers=report.monotony_result.all_openers,
        total_sentences=report.monotony_result.total_sentences,
        monotony_score=report.monotony_result.monotony_score,
    )

    # Template results
    filtered_templates = _filter_flagged_items(
        report.template_result.flagged_templates, target_sents,
        report.template_result.total_sentences,
        cls=FlaggedTemplate, label_field="template",
    )
    filtered_template = TemplateResult(
        flagged_templates=filtered_templates,
        all_templates=report.template_result.all_templates,
        total_sentences=report.template_result.total_sentences,
        unique_templates=report.template_result.unique_templates,
        repetition_score=report.template_result.repetition_score,
    )

    # Not-but results
    filtered_not_but = [nb for nb in report.not_but_result if nb.get("sentence", "") in target_sents]

    return AuditReport(
        cliche_result=filtered_cliche,
        monotony_result=filtered_monotony,
        template_result=filtered_template,
        not_but_result=filtered_not_but,
    )


# ── Audit with multi-message context ─────────────────────────────────────────

def _build_audit_text(draft: str, previous_assistant_msgs: list[str]) -> str:
    """Concatenate previous assistant messages (oldest→newest) with the current
    *draft* so that repetition detectors can see cross-message patterns."""
    if not previous_assistant_msgs:
        return draft
    context = "\n\n".join(reversed(previous_assistant_msgs))
    return context + "\n\n" + draft


def _run_contextual_audit(
    draft: str, phrase_bank: list[list[str]], previous_assistant_msgs: list[str],
) -> tuple[AuditReport, str]:
    """Run audit on *draft* with cross-message context, then filter results
    to only include issues in the draft itself.  Returns (report, report_text)."""
    full_text = _build_audit_text(draft, previous_assistant_msgs)
    raw_report = run_audit(full_text, phrase_bank)
    filtered = filter_audit_report_to_text(raw_report, draft)
    return filtered, format_report(filtered)


# ── Quote normalisation & patching ────────────────────────────────────────────

_QUOTE_MAP = str.maketrans({
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
    "\u2013": "-", "\u2014": "-",
})


def _normalize_quotes(text: str) -> str:
    return text.translate(_QUOTE_MAP)


def apply_patches(draft: str, patches: list[dict]) -> tuple[str, list[str]]:
    """Apply search/replace patches to *draft*.  Returns (updated_draft, error_messages)."""
    errors: list[str] = []
    logger.debug("Applying %d patches to draft (%d chars)", len(patches), len(draft))

    for i, p in enumerate(patches):
        search = p.get("search", "")
        replace = p.get("replace", "")
        if not search:
            logger.debug("Patch %d: empty search string, skipping", i)
            continue
        if search == replace:
            err = f"Error: Patch {i} is a no-op (search === replace). You must provide different replacement text."
            errors.append(err)
            continue

        count = draft.count(search)

        # Fallback: try with normalised quotes when exact match fails
        if count == 0:
            norm_search = _normalize_quotes(search)
            norm_draft = _normalize_quotes(draft)
            norm_count = norm_draft.count(norm_search)
            if norm_count == 1:
                pos = norm_draft.index(norm_search)
                original_substr = draft[pos: pos + len(norm_search)]
                if len(original_substr) == len(norm_search):
                    draft = draft[:pos] + replace + draft[pos + len(original_substr):]
                    logger.debug("Patch %d OK (quote-normalized): %r → %r", i, search[:60], replace[:60])
                    continue
            elif norm_count > 1:
                errors.append(f"Error: Multiple matches ({norm_count}) for '{search[:80]}' (after quote normalization). Use more context.")
                continue
            errors.append(f"Error: '{search[:80]}' not found in draft.")

        elif count > 1:
            errors.append(f"Error: Multiple matches ({count}) for '{search[:80]}'. Use more context.")
        else:
            draft = draft.replace(search, replace, 1)
            logger.debug("Patch %d OK: %r → %r", i, search[:60], replace[:60])

    logger.debug("Patch application done: %d errors out of %d patches", len(errors), len(patches))
    return draft, errors


# ── Refine pass (ReAct loop) ──────────────────────────────────────────────────

async def refine_pass(
    client: LLMClient, prefix: list[dict], effective_msg: str, draft: str,
    settings: dict, phrase_bank: list[list[str]],
    audit_enabled: bool = True,
    length_guard: dict | None = None,
    enabled_tools: dict | None = None,
    kv_tracker=None,
    reasoning_on: bool = True,
) -> AsyncIterator[dict]:
    """ReAct-style refinement loop with optional audit and/or length guard.

    Yields:
        {"type": "reasoning", "delta": str}                      — reasoning chunks per iteration
        {"type": "done", "draft": str|None, "debug": str, "elapsed": int}
    """
    t0 = time.monotonic()
    debug_parts: list[str] = []

    # Collect previous assistant messages for cross-message context
    assistant_messages: list[str] = []
    if audit_enabled:
        for msg in reversed(prefix):
            if msg.get("role") == "assistant":
                assistant_messages.append(msg.get("content", ""))
                if len(assistant_messages) >= 2:
                    break

    # ── Initial audit
    if audit_enabled:
        logger.info("Refine: audit on draft (%d chars), %d previous messages, %d phrase groups",
                     len(draft), len(assistant_messages), len(phrase_bank))
        report, report_text = _run_contextual_audit(draft, phrase_bank, assistant_messages)
        logger.info("Refine: initial audit — %d issues (cliches=%d, openers=%d, templates=%d)",
                     report.total_issues, report.cliche_result.flagged_count,
                     len(report.monotony_result.flagged_openers),
                     len(report.template_result.flagged_templates))
        debug_parts.append(f"Initial audit ({report.total_issues} issues):\n{report_text}")
    else:
        report = AuditReport.clean()
        report_text = ""
        logger.info("Refine: audit disabled, skipping scanners")

    # ── Length guard
    length_guard_triggered = False
    length_guard_instruction = ""

    # Start from the same enabled-tool set used by the director and writer
    # passes so the KV-cache prefix stays aligned.  REFINE_APPLY_PATCH_TOOL
    # is already included when audit_enabled is True (the flag requires it).
    # REFINE_REWRITE_TOOL is appended conditionally below when the length
    # guard fires — an acceptable cache-miss because it rarely triggers.
    refine_tools: list[dict] = enabled_schemas(enabled_tools)

    if length_guard and length_guard.get("enabled"):
        word_count = len(draft.split())
        max_words = length_guard.get("max_words", 280)
        max_paragraphs = length_guard.get("max_paragraphs", 3)
        if word_count > max_words:
            length_guard_triggered = True
            length_guard_instruction = LENGTH_GUARD_INSTRUCTIONS.format(
                word_count=word_count, max_paragraphs=max_paragraphs, max_words=max_words
            )
            if REFINE_REWRITE_TOOL not in refine_tools:
                refine_tools.append(REFINE_REWRITE_TOOL)
            logger.info("Refine: length guard triggered (word_count=%d > max_words=%d)", word_count, max_words)
            debug_parts.append(f"Length guard triggered: {word_count} words (max {max_words})")

    if report.is_clean and not length_guard_triggered:
        logger.info("Refine: audit clean and no length guard, skipping LLM loop")
        yield {"type": "done", "draft": None, "debug": "\n---\n".join(debug_parts), "elapsed": int((time.monotonic() - t0) * 1000)}
        return

    if not refine_tools:
        logger.info("Refine: no refine tools applicable, skipping LLM loop")
        yield {"type": "done", "draft": None, "debug": "\n---\n".join(debug_parts), "elapsed": int((time.monotonic() - t0) * 1000)}
        return

    # ── Build message context
    final_prompt = _build_refine_prompt(
        audit_enabled and not report.is_clean, report_text,
        length_guard_triggered, length_guard_instruction,
    )

    logger.info(final_prompt)

    msgs = prefix + [
        {"role": "user", "content": effective_msg},
        {"role": "assistant", "content": draft},
        {"role": "user", "content": final_prompt},
    ]

    current_draft = draft
    prev_issues = report.total_issues

    # ── ReAct loop
    for iteration in range(MAX_REFINE_ITERATIONS):
        logger.info("Refine iteration %d/%d, %d issues remaining", iteration + 1, MAX_REFINE_ITERATIONS, report.total_issues)
        if kv_tracker is not None and iteration == 0:
            kv_tracker.record("refine", msgs, refine_tools)
        try:
            reasoning_params = reasoning_cfg(False) if not reasoning_on else (reasoning_config_for_schemas(refine_tools) or reasoning_cfg(True))
            if not reasoning_params["reasoning"].get("enabled", True):
                logger.info("Refine iteration %d: reasoning disabled", iteration + 1)

            resp: dict = {}
            try:
                async for event in client.complete(
                    messages=msgs,
                    model=settings["model_name"],
                    tools=refine_tools,
                    tool_choice=_pick_tool_choice(length_guard_triggered, report, audit_enabled),
                    temperature=0.25,
                    max_tokens=8192,
                    **reasoning_params,
                ):
                    if event["type"] == "reasoning":
                        yield {"type": "reasoning", "delta": event["delta"]}
                    elif event["type"] == "done":
                        resp = event["message"]
            except Exception as llm_err:
                logger.error("Refine iteration %d: client.complete() raised %s: %s",
                             iteration + 1, type(llm_err).__name__, llm_err, exc_info=True)
                raise

            raw = json.dumps(resp, default=str)
            debug_parts.append(f"Iteration {iteration + 1} response:\n{raw}")

            finish_reason = resp.get("finish_reason") or resp.get("stop_reason")
            if finish_reason:
                logger.info("Refine iteration %d: finish_reason=%s", iteration + 1, finish_reason)

            parsed = parse_tool_calls(resp)
            if not parsed:
                logger.info("Refine iteration %d: no tool call, stopping", iteration + 1)
                break

            # ── Handle refine_rewrite
            rewrite_call = next((tc for tc in parsed if tc["name"] == "refine_rewrite"), None)
            if rewrite_call:
                rewritten = rewrite_call.get("arguments", {}).get("rewritten_text", "").strip()
                if not rewritten:
                    logger.info("Refine iteration %d: empty rewrite, stopping", iteration + 1)
                    break
                pre_len = len(current_draft)
                current_draft = rewritten
                length_guard_triggered = False
                logger.info("Refine iteration %d: rewrite applied, draft %d→%d chars", iteration + 1, pre_len, len(current_draft))
                debug_parts.append(f"Iteration {iteration + 1}: rewrite applied ({pre_len}→{len(current_draft)} chars)")

                if audit_enabled:
                    report, report_text = _run_contextual_audit(current_draft, phrase_bank, assistant_messages)
                    debug_parts.append(f"Post-rewrite audit ({report.total_issues} issues):\n{report_text}")
                else:
                    report = AuditReport.clean()
                    report_text = ""

                if report.is_clean:
                    break
                refine_tools = [REFINE_APPLY_PATCH_TOOL] if audit_enabled else []
                prev_issues = report.total_issues
                msgs[-2] = {"role": "assistant", "content": current_draft}
                msgs[-1] = {"role": "user", "content": _build_refine_prompt(
                    audit_enabled, report_text,
                    length_guard_triggered, length_guard_instruction,
                )}
                continue

            # ── Handle refine_apply_patch
            patch_call = next((tc for tc in parsed if tc["name"] == "refine_apply_patch"), None)
            if not patch_call:
                logger.info("Refine iteration %d: unrecognised tool call, stopping", iteration + 1)
                break

            patches = patch_call.get("arguments", {}).get("patches", [])
            if not patches:
                logger.info("Refine iteration %d: empty patches, stopping", iteration + 1)
                break

            pre_len = len(current_draft)
            current_draft, errors = apply_patches(current_draft, patches)
            logger.info("Refine iteration %d: applied %d patches, draft %d→%d chars", iteration + 1, len(patches), pre_len, len(current_draft))
            for e in errors:
                logger.warning("Refine iteration %d patch error: %s", iteration + 1, e)

            report, report_text = _run_contextual_audit(current_draft, phrase_bank, assistant_messages)
            logger.info("Refine iteration %d: post-audit — %d issues", iteration + 1, report.total_issues)
            debug_parts.append(f"Post-iteration {iteration + 1} audit ({report.total_issues} issues):\n{report_text}")

            if report.is_clean:
                if not length_guard_triggered:
                    break
                logger.info("Refine: audit clean, length guard still pending — queuing rewrite")
                refine_tools = [REFINE_REWRITE_TOOL]

            if report.total_issues >= prev_issues:
                logger.info("Refine: no progress (%d → %d issues), stopping", prev_issues, report.total_issues)
                break
            prev_issues = report.total_issues

            # Append recap for next iteration
            _append_iteration_context(msgs, resp, patches, errors, report_text)

        except Exception as e:
            logger.error("Refine iteration %d failed: %s", iteration + 1, e, exc_info=True)
            debug_parts.append(f"Iteration {iteration + 1} error: {e}")
            break
    else:
        logger.warning("Refine: hit max iterations (%d) with %d issues remaining", MAX_REFINE_ITERATIONS, report.total_issues)

    elapsed = int((time.monotonic() - t0) * 1000)
    changed = current_draft != draft
    logger.info("Refine: done in %dms, changed=%s, final_draft=%d chars", elapsed, changed, len(current_draft))
    yield {"type": "done", "draft": current_draft if changed else None, "debug": "\n---\n".join(debug_parts), "elapsed": elapsed}


# ── Helpers (private) ─────────────────────────────────────────────────────────

def _build_refine_prompt(
    has_audit_issues: bool, report_text: str,
    length_guard_triggered: bool, length_guard_instruction: str,
) -> str:
    """Assemble the refinement instruction sent as the final user message.

    The preamble is *always* included so the model knows it is the
    Refinement Agent and that the assistant message above is the draft.
    Audit rules and/or length-guard instructions are appended as needed.
    """
    parts = [REFINE_PREAMBLE]
    if has_audit_issues:
        parts.append(REFINE_AUDIT_INSTRUCTIONS)
        parts.append(report_text)
    if length_guard_triggered:
        parts.append(length_guard_instruction)
    return "\n\n".join(parts)


def _pick_tool_choice(length_guard_triggered: bool, report: AuditReport, audit_enabled: bool):
    """Determine the tool_choice parameter for the refine LLM call."""
    if length_guard_triggered and report.is_clean:
        return {"type": "function", "function": {"name": "refine_rewrite"}}
    if length_guard_triggered:
        return "auto"
    if audit_enabled:
        return TOOLS["refine_apply_patch"]["choice"]
    return "auto"


def _append_iteration_context(msgs: list[dict], resp: dict, patches: list[dict], errors: list[str], report_text: str):
    """Append assistant recap and user tool-result turns for the next iteration."""
    reasoning = resp.get("content", "") or ""
    reasoning_content = resp.get("reasoning_content", "") or ""

    tool_name = "refine_apply_patch"
    reasoning_enabled = TOOLS.get(tool_name, {}).get("reasoning_enabled", True)

    patch_summary = "; ".join(
        f"replaced \"{p.get('search', '')[:40]}…\"" for p in patches if p.get("search") != p.get("replace")
    ) or "no effective changes"

    if reasoning_enabled and (reasoning or reasoning_content):
        combined = (reasoning + "\n" + reasoning_content).strip()
        assistant_recap = combined + "\n\n" + f"[Applied patches: {patch_summary}]"
    else:
        assistant_recap = f"[Applied patches: {patch_summary}]"

    msgs.append({"role": "assistant", "content": assistant_recap})

    tool_response = "\n".join(errors) + "\n\n" + report_text if errors else report_text
    msgs.append({"role": "user", "content": f"[Tool result — updated audit after your patches]\n{tool_response}"})
