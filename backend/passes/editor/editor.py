"""
editor.py — Editor pass: the post-processing phase that runs a
ReAct-style LLM loop that fixes audit issues in Writer's output.
"""

from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator

from .audit import run_audit, format_report, AuditReport
from .slop_detector import DetectionResult
from .opening_monotony import FlaggedOpener, MonotonyResult, _split_sentences
from .template_repetition import FlaggedTemplate, TemplateResult
from ...llm_client import LLMClient, parse_tool_calls, reasoning_cfg
from ...tool_defs import (
    TOOLS,
    EDITOR_APPLY_PATCH_TOOL,
    EDITOR_REWRITE_TOOL,
    EDITOR_PREAMBLE,
    EDITOR_PATCH_INSTRUCTIONS,
    EDITOR_REWRITE_INSTRUCTIONS,
    EDITOR_BOTH_INSTRUCTIONS,
    STRUCTURAL_REWRITE_INSTRUCTIONS,
    LENGTH_GUARD_INSTRUCTIONS,
    MAX_EDITOR_ITERATIONS,
    enabled_schemas,
)

logger = logging.getLogger(__name__)


# ── Audit-report filtering ────────────────────────────────────────────────────


def _split_target_sentences(target_text: str) -> set[str]:
    """Split *target_text* into a sentence set using the same heuristic as the detectors."""
    return set(_split_sentences(target_text))


def _filter_flagged_items(
    items, sentences: set[str], total: int, *, cls, label_field: str
):
    """Shared helper: filter a list of FlaggedOpener or FlaggedTemplate to
    only include sentences present in *sentences*.

    Returns a list of *cls* instances (with adjusted count / fraction).
    """
    filtered = []
    for item in items:
        kept = [s for s in item.sentences if s in sentences]
        if kept:
            extra = {
                k: v
                for k, v in vars(item).items()
                if k not in (label_field, "count", "fraction", "sentences")
            }
            filtered.append(
                cls(
                    **{label_field: getattr(item, label_field)},
                    count=len(kept),
                    fraction=len(kept) / total if total > 0 else 0.0,
                    sentences=kept,
                    **extra,
                )
            )
    return filtered


def filter_audit_report_to_text(report: AuditReport, target_text: str) -> AuditReport:
    """Narrow an audit report to only flag sentences that appear in *target_text*.

    Used when the audit ran on concatenated text (current + previous messages)
    but we only want to surface issues in the latest message.
    """
    target_sents = _split_target_sentences(target_text)

    # Cliché results — slop_detector splits only on [.!?] while _split_target_sentences
    # also splits after quote chars, so sentences may not match as set members.
    # Use substring containment instead, which is guaranteed correct since the
    # detector can only flag sentences it found within the text.
    filtered_fs = [
        fs
        for fs in report.cliche_result.flagged_sentences
        if fs.sentence in target_text
    ]
    filtered_cliche = DetectionResult(
        flagged_sentences=filtered_fs,
        unique_cliches=report.cliche_result.unique_cliches,
        total_sentences=report.cliche_result.total_sentences,
        flagged_count=len(filtered_fs),
    )

    # Opener results
    filtered_openers = _filter_flagged_items(
        report.monotony_result.flagged_openers,
        target_sents,
        report.monotony_result.total_sentences,
        cls=FlaggedOpener,
        label_field="opener",
    )
    filtered_monotony = MonotonyResult(
        flagged_openers=filtered_openers,
        all_openers=report.monotony_result.all_openers,
        total_sentences=report.monotony_result.total_sentences,
        monotony_score=report.monotony_result.monotony_score,
    )

    # Template results
    filtered_templates = _filter_flagged_items(
        report.template_result.flagged_templates,
        target_sents,
        report.template_result.total_sentences,
        cls=FlaggedTemplate,
        label_field="template",
    )
    filtered_template = TemplateResult(
        flagged_templates=filtered_templates,
        all_templates=report.template_result.all_templates,
        total_sentences=report.template_result.total_sentences,
        unique_templates=report.template_result.unique_templates,
        repetition_score=report.template_result.repetition_score,
    )

    # Not-but results — same mismatch issue as clichés (contrastive_negation also
    # splits only on [.!?]), so use substring containment here too.
    filtered_not_but = [
        nb for nb in report.not_but_result if nb.get("sentence", "") in target_text
    ]

    # Structural repetition is a cross-message check, so it's always relevant
    # when comparing the draft to previous messages. We keep it unfiltered.

    return AuditReport(
        cliche_result=filtered_cliche,
        monotony_result=filtered_monotony,
        template_result=filtered_template,
        not_but_result=filtered_not_but,
        structural_repetition_result=report.structural_repetition_result,
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
    draft: str,
    phrase_bank: list[list[str]],
    previous_assistant_msgs: list[str],
) -> tuple[AuditReport, str]:
    """Run audit on *draft* with cross-message context, then filter results
    to only include issues in the draft itself.  Returns (report, report_text)."""
    full_text = _build_audit_text(draft, previous_assistant_msgs)
    # run_audit will append the current text to assistant_messages internally
    raw_report = run_audit(
        full_text,
        phrase_bank,
        assistant_messages=previous_assistant_msgs,
        structural_text=draft,
    )
    filtered = filter_audit_report_to_text(raw_report, draft)
    return filtered, format_report(filtered)


# ── Quote normalisation & patching ────────────────────────────────────────────

_QUOTE_MAP = str.maketrans(
    {
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u2013": "-",
        "\u2014": "-",
    }
)


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
                original_substr = draft[pos : pos + len(norm_search)]
                if len(original_substr) == len(norm_search):
                    draft = draft[:pos] + replace + draft[pos + len(original_substr) :]
                    logger.debug(
                        "Patch %d OK (quote-normalized): %r → %r",
                        i,
                        search[:60],
                        replace[:60],
                    )
                    continue
            elif norm_count > 1:
                errors.append(
                    f"Error: Multiple matches ({norm_count}) for {search[:80]!r} (after quote normalization). Use more context."
                )
                continue
            errors.append(f"Error: {search[:80]!r} not found in draft.")

        elif count > 1:
            errors.append(
                f"Error: Multiple matches ({count}) for {search[:80]!r}. Use more context."
            )
        else:
            draft = draft.replace(search, replace, 1)
            logger.debug("Patch %d OK: %r → %r", i, search[:60], replace[:60])

    logger.debug(
        "Patch application done: %d errors out of %d patches", len(errors), len(patches)
    )
    return draft, errors


# ── Editor pass (ReAct loop) ─────────────────────────────────────────────────


async def editor_pass(
    client: LLMClient,
    prefix: list[dict],
    effective_msg: str,
    draft: str,
    settings: dict,
    phrase_bank: list[list[str]],
    audit_enabled: bool = True,
    length_guard: dict | None = None,
    enabled_tools: dict | None = None,
    kv_tracker=None,
    reasoning_on: bool = False,  # If true, use structured tool-use message format (role=tool) for iteration feedback; non-thinking models get a synthetic recap instead
) -> AsyncIterator[dict]:
    """ReAct-style editor loop with optional audit and/or length guard.

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
                if len(assistant_messages) >= 3:
                    break

    # ── Initial audit
    if audit_enabled:
        logger.info(
            "Editor: audit on draft (%d chars), %d previous messages, %d phrase groups",
            len(draft),
            len(assistant_messages),
            len(phrase_bank),
        )
        report, report_text = _run_contextual_audit(
            draft, phrase_bank, assistant_messages
        )
        structural_issues = (
            1
            if report.structural_repetition_result
            and report.structural_repetition_result.is_repetitive
            else 0
        )
        logger.info(
            "Editor: initial audit — %d issues (cliches=%d, openers=%d, templates=%d, structural=%d)",
            report.total_issues,
            report.cliche_result.flagged_count,
            len(report.monotony_result.flagged_openers),
            len(report.template_result.flagged_templates),
            structural_issues,
        )
        debug_parts.append(
            f"Initial audit ({report.total_issues} issues):\n{report_text}"
        )
    else:
        report = AuditReport.clean()
        report_text = ""
        logger.info("Editor: audit disabled, skipping scanners")

    # ── Length guard
    length_guard_triggered = False
    length_guard_instruction = ""

    # Start from the same enabled-tool set used by the director and writer
    # passes so the KV-cache prefix stays aligned.  EDITOR_APPLY_PATCH_TOOL
    # is included when audit_enabled is True; EDITOR_REWRITE_TOOL is included
    # when length_guard is enabled — both injected by the orchestrator into
    # enabled_tools before reaching this pass.
    editor_tools: list[dict] = enabled_schemas(enabled_tools)

    if length_guard and length_guard.get("enabled"):
        word_count = len(draft.split())
        max_words = length_guard.get("max_words", 280)
        max_paragraphs = length_guard.get("max_paragraphs", 3)
        if word_count > max_words:
            length_guard_triggered = True
            length_guard_instruction = LENGTH_GUARD_INSTRUCTIONS.format(
                word_count=word_count,
                max_paragraphs=max_paragraphs,
                max_words=max_words,
            )
            logger.info(
                "Editor: length guard triggered (word_count=%d > max_words=%d)",
                word_count,
                max_words,
            )
            debug_parts.append(
                f"Length guard triggered: {word_count} words (max {max_words})"
            )

    if report.is_clean and not length_guard_triggered:
        logger.info("Editor: audit clean and no length guard, skipping LLM loop")
        yield {
            "type": "done",
            "draft": None,
            "debug": "\n---\n".join(debug_parts),
            "elapsed": int((time.monotonic() - t0) * 1000),
        }
        return

    if not editor_tools:
        logger.info("Editor: no editor tools applicable, skipping LLM loop")
        yield {
            "type": "done",
            "draft": None,
            "debug": "\n---\n".join(debug_parts),
            "elapsed": int((time.monotonic() - t0) * 1000),
        }
        return

    # ── Build message context
    final_prompt = _build_editor_prompt(
        audit_enabled and not report.is_clean,
        report_text,
        length_guard_triggered,
        length_guard_instruction,
        structural_rewrite=_structural_rewrite_needed(report),
    )

    logger.info(final_prompt)

    msgs = prefix + [
        {"role": "user", "content": effective_msg},
        {"role": "assistant", "content": draft},
        {"role": "user", "content": final_prompt},
    ]

    current_draft = draft
    prev_issues = report.total_issues
    all_calls: list[dict] = []

    # ── ReAct loop
    for iteration in range(MAX_EDITOR_ITERATIONS):
        if client.is_aborted:
            logger.info("Editor: abort signal detected at iteration %d, stopping", iteration + 1)
            break
        logger.debug(
            "Editor iteration %d/%d, %d issues remaining",
            iteration + 1,
            MAX_EDITOR_ITERATIONS,
            report.total_issues,
        )
        if kv_tracker is not None and iteration == 0:
            kv_tracker.record("editor", msgs, editor_tools)
        try:
            reasoning_params = (
                reasoning_cfg(False) if not reasoning_on else reasoning_cfg(True)
            )
            if not reasoning_params["reasoning"].get("enabled", True):
                logger.info("Editor iteration %d: reasoning disabled", iteration + 1)

            logger.info(
                "Editor iteration %d: sending %d messages to LLM:\n%s",
                iteration + 1,
                len(msgs),
                json.dumps(msgs, default=str, indent=2),
            )

            resp: dict = {}
            try:
                async for event in client.complete(
                    messages=msgs,
                    model=settings["model_name"],
                    tools=editor_tools,
                    tool_choice=_pick_tool_choice(
                        length_guard_triggered, report, audit_enabled
                    ),
                    temperature=0.25,
                    max_tokens=8192,
                    **reasoning_params,
                ):
                    if event["type"] == "reasoning":
                        yield {"type": "reasoning", "delta": event["delta"]}
                    elif event["type"] == "done":
                        resp = event["message"]
            except Exception as llm_err:
                logger.error(
                    "Editor iteration %d: client.complete() raised %s: %s",
                    iteration + 1,
                    type(llm_err).__name__,
                    llm_err,
                    exc_info=True,
                )
                raise

            raw = json.dumps(resp, default=str)
            debug_parts.append(f"Iteration {iteration + 1} response:\n{raw}")

            finish_reason = resp.get("finish_reason") or resp.get("stop_reason")
            if finish_reason:
                logger.info(
                    "Editor iteration %d: finish_reason=%s",
                    iteration + 1,
                    finish_reason,
                )

            parsed = parse_tool_calls(resp)
            if not parsed:
                logger.info(
                    "Editor iteration %d: no tool call (resp=%s), stopping",
                    iteration + 1,
                    "empty" if not resp else f"finish_reason={finish_reason}",
                )
                break
            all_calls.extend(parsed)

            # ── Handle editor_rewrite
            rewrite_call = next(
                (tc for tc in parsed if tc["name"] == "editor_rewrite"), None
            )
            if rewrite_call:
                rewritten = (
                    rewrite_call.get("arguments", {}).get("rewritten_text", "").strip()
                )
                if not rewritten:
                    logger.info(
                        "Editor iteration %d: empty rewrite, stopping", iteration + 1
                    )
                    break
                pre_len = len(current_draft)
                current_draft = rewritten
                length_guard_triggered = False
                logger.info(
                    "Editor iteration %d: rewrite applied, draft %d→%d chars",
                    iteration + 1,
                    pre_len,
                    len(current_draft),
                )
                debug_parts.append(
                    f"Iteration {iteration + 1}: rewrite applied ({pre_len}→{len(current_draft)} chars)"
                )

                if audit_enabled:
                    report, report_text = _run_contextual_audit(
                        current_draft, phrase_bank, assistant_messages
                    )
                    debug_parts.append(
                        f"Post-rewrite audit ({report.total_issues} issues):\n{report_text}"
                    )
                else:
                    report = AuditReport.clean()
                    report_text = ""

                if report.is_clean:
                    break
                if _structural_rewrite_needed(report):
                    editor_tools = [EDITOR_REWRITE_TOOL]
                elif audit_enabled:
                    editor_tools = [EDITOR_APPLY_PATCH_TOOL]
                else:
                    editor_tools = []
                prev_issues = report.total_issues
                if reasoning_on:
                    rewrite_tool_calls = resp.get("tool_calls", [])
                    msgs.append(
                        {
                            "role": "assistant",
                            "content": resp.get("content") or "",
                            "tool_calls": rewrite_tool_calls,
                        }
                    )
                    if rewrite_tool_calls:
                        msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": rewrite_tool_calls[0].get("id", ""),
                                "content": report_text,
                            }
                        )
                else:
                    msgs[-2] = {"role": "assistant", "content": current_draft}
                    msgs[-1] = {
                        "role": "user",
                        "content": _build_editor_prompt(
                            audit_enabled and not report.is_clean,
                            report_text,
                            length_guard_triggered,
                            length_guard_instruction,
                            structural_rewrite=_structural_rewrite_needed(report),
                        ),
                    }
                continue

            # ── Handle editor_apply_patch
            patch_call = next(
                (tc for tc in parsed if tc["name"] == "editor_apply_patch"), None
            )
            if not patch_call:
                logger.info(
                    "Editor iteration %d: unrecognised tool call, stopping",
                    iteration + 1,
                )
                break

            patches = patch_call.get("arguments", {}).get("patches", [])
            if not patches:
                logger.info(
                    "Editor iteration %d: empty patches, stopping", iteration + 1
                )
                break

            pre_len = len(current_draft)
            current_draft, errors = apply_patches(current_draft, patches)
            logger.info(
                "Editor iteration %d: applied %d patches, draft %d→%d chars",
                iteration + 1,
                len(patches),
                pre_len,
                len(current_draft),
            )
            for e in errors:
                logger.warning("Editor iteration %d patch error: %s", iteration + 1, e)

            report, report_text = _run_contextual_audit(
                current_draft, phrase_bank, assistant_messages
            )
            logger.info(
                "Editor iteration %d: post-audit — %d issues",
                iteration + 1,
                report.total_issues,
            )
            debug_parts.append(
                f"Post-iteration {iteration + 1} audit ({report.total_issues} issues):\n{report_text}"
            )

            if report.is_clean:
                if not length_guard_triggered:
                    break
                logger.info(
                    "Editor: audit clean, length guard still pending — queuing rewrite"
                )
                editor_tools = [EDITOR_REWRITE_TOOL]

            if report.total_issues >= prev_issues:
                logger.info(
                    "Editor: no progress (%d → %d issues), stopping",
                    prev_issues,
                    report.total_issues,
                )
                break
            prev_issues = report.total_issues

            # Append recap for next iteration
            _append_iteration_context(
                msgs, resp, patches, errors, report_text, reasoning_on=reasoning_on
            )

        except Exception as e:
            logger.error(
                "Editor iteration %d failed: %s", iteration + 1, e, exc_info=True
            )
            debug_parts.append(f"Iteration {iteration + 1} error: {e}")
            raise
    else:
        logger.warning(
            "Editor: hit max iterations (%d) with %d issues remaining",
            MAX_EDITOR_ITERATIONS,
            report.total_issues,
        )

    elapsed = int((time.monotonic() - t0) * 1000)
    changed = current_draft != draft
    logger.info(
        "Editor: done in %dms, changed=%s, final_draft=%d chars",
        elapsed,
        changed,
        len(current_draft),
    )
    yield {
        "type": "done",
        "draft": current_draft if changed else None,
        "debug": "\n---\n".join(debug_parts),
        "elapsed": elapsed,
        "tool_calls": all_calls,
    }


# ── Helpers (private) ─────────────────────────────────────────────────────────


def _build_editor_prompt(
    has_audit_issues: bool,
    report_text: str,
    length_guard_triggered: bool,
    length_guard_instruction: str,
    structural_rewrite: bool = False,
) -> str:
    """Assemble the editor instruction sent as the final user message.

    The preamble is *always* included so the model knows it is the
    Editor Agent and that the assistant message above is the draft.
    Audit rules and/or length-guard instructions are appended as needed.
    """
    parts = [EDITOR_PREAMBLE]
    rewrite_triggered = length_guard_triggered or structural_rewrite

    if rewrite_triggered:
        parts.append(EDITOR_REWRITE_INSTRUCTIONS)
        if has_audit_issues:
            parts.append(report_text)
        if structural_rewrite:
            parts.append(STRUCTURAL_REWRITE_INSTRUCTIONS)
        if length_guard_triggered:
            parts.append(length_guard_instruction)
        if has_audit_issues and length_guard_triggered:
            parts.append(EDITOR_BOTH_INSTRUCTIONS)
    elif has_audit_issues:
        parts.append(EDITOR_PATCH_INSTRUCTIONS)
        parts.append(report_text)

    return "\n\n".join(parts)


def _structural_rewrite_needed(report: AuditReport) -> bool:
    return (
        report.structural_repetition_result is not None
        and report.structural_repetition_result.is_repetitive
    )


def _pick_tool_choice(
    length_guard_triggered: bool, report: AuditReport, audit_enabled: bool
):
    """Determine the tool_choice parameter for the editor LLM call."""
    if length_guard_triggered or _structural_rewrite_needed(report):
        return {"type": "function", "function": {"name": "editor_rewrite"}}
    if audit_enabled:
        return TOOLS["editor_apply_patch"]["choice"]
    return "auto"


def _append_iteration_context(
    msgs: list[dict],
    resp: dict,
    patches: list[dict],
    errors: list[str],
    report_text: str,
    *,
    reasoning_on: bool,
):
    """Append assistant recap and tool-result turns for the next iteration.

    reasoning_on=True: use structured tool-use format so the model can see its
    exact tool call and the remaining issues in the form it was trained on.
    reasoning_on=False: use a human-readable synthetic recap which is more
    reliable for non-thinking models.
    """
    tool_response = ("\n".join(errors) + "\n\n" if errors else "") + report_text
    if reasoning_on:
        tool_calls = resp.get("tool_calls", [])
        msgs.append(
            {
                "role": "assistant",
                "content": resp.get("content") or "",
                "tool_calls": tool_calls,
            }
        )
        for tc in tool_calls:
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_response,
                }
            )
    else:
        reasoning = resp.get("content", "") or ""
        reasoning_content = resp.get("reasoning_content", "") or ""
        patch_summary = (
            "; ".join(
                f"replaced \"{p.get('search', '')[:40]}…\""
                for p in patches
                if p.get("search") != p.get("replace")
            )
            or "no effective changes"
        )
        if reasoning or reasoning_content:
            combined = (reasoning + "\n" + reasoning_content).strip()
            assistant_recap = combined + "\n\n" + f"[Applied patches: {patch_summary}]"
        else:
            assistant_recap = f"[Applied patches: {patch_summary}]"
        msgs.append({"role": "assistant", "content": assistant_recap})
        msgs.append(
            {
                "role": "user",
                "content": f"[Tool result — updated audit after your patches]\n{tool_response}",
            }
        )
