"""
editor.py — Editor pass: the post-processing phase that runs a
ReAct-style LLM loop that fixes audit issues in Writer's output.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Sequence

from ...analysis import AuditReport, DetectionResult, format_report, run_audit
from .feedback import FeedbackResult, feedback_step

if TYPE_CHECKING:
    from ...database.models import PhraseGroup
    from ...pipeline_state import TurnState, _PipelineConfig
from ...inference import CachedBase
from ...inference import _KVCacheTracker
from ...inference import LLMClient, parse_tool_calls, reasoning_cfg
from ...core import AssistantToolMessage, ContentPart, WireMessage
from ...inference import build_editor_prompt
from ...inference import (
    TOOLS,
    build_feedback_tool,
)
from ...core import extract_hyperparams
from ...analysis import (
    FlaggedOpener,
    FlaggedTemplate,
    MonotonyResult,
    TemplateResult,
    split_narration_sentences,
)
from .length_guard import LengthGuard, evaluate_length_guard

logger = logging.getLogger(__name__)

MAX_EDITOR_ITERATIONS = 3


# ── Feedback gating + tool override ───────────────────────────────────────────


def _feedback_active(
    settings: Mapping[str, Any],
    feedback_fragments: Sequence[Mapping[str, Any]],
    *,
    agent_on: bool,
) -> bool:
    """Feedback runs only when its flag is on, the agent is on, AND an enabled
    feedback fragment exists.

    The feedback step is an editor sub-step, so this gate lives with the editor.
    *agent_on* is passed in (rather than recomputed) so the orchestrator's
    ``agent_enabled`` stays the single source of truth — mirroring how
    ``resolve_length_guard`` takes ``agent_on``.
    """
    return agent_on and bool(settings.get("feedback_enabled", 0)) and bool(feedback_fragments)


def build_feedback_override(feedback_fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Build the ``give_feedback`` dynamic-tool schema from *feedback_fragments*.

    Thin wrapper over :func:`~backend.tool_registry.build_feedback_tool` so the
    orchestrator's tools-blob composition (``_build_writer_tools_blob``) reaches
    the give_feedback schema through the editor module rather than importing the
    schema builder directly — symmetric to ``build_direct_scene_override``.
    """
    return build_feedback_tool(feedback_fragments)


# ── Audit-report filtering ────────────────────────────────────────────────────


def _split_target_sentences(target_text: str) -> set[str]:
    """Split *target_text* into a sentence set using the same heuristic as the detectors."""
    return set(split_narration_sentences(target_text))


def _filter_flagged_items(items, sentences: set[str], total: int, *, cls, label_field: str):
    """Shared helper: filter a list of FlaggedOpener or FlaggedTemplate to
    only include sentences present in *sentences*.

    Returns a list of *cls* instances (with adjusted count / fraction).
    """
    filtered = []
    for item in items:
        kept = [s for s in item.sentences if s in sentences]
        if kept:
            extra = {k: v for k, v in vars(item).items() if k not in (label_field, "count", "fraction", "sentences")}
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
    filtered_fs = [fs for fs in report.cliche_result.flagged_sentences if fs.sentence in target_text]
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
    filtered_not_but = [nb for nb in report.not_but_result if nb.get("sentence", "") in target_text]

    # Structural repetition and exact phrase repetition are cross-message checks,
    # so they're always relevant when comparing the draft to previous messages.
    # Phrase repetition already focuses on the draft via require_last_message, so
    # we keep both unfiltered.

    return AuditReport(
        cliche_result=filtered_cliche,
        monotony_result=filtered_monotony,
        template_result=filtered_template,
        not_but_result=filtered_not_but,
        phrase_result=report.phrase_result,
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
    phrase_bank: list[PhraseGroup],
    previous_assistant_msgs: list[str],
    audit_toggles: dict | None = None,
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
        audit_toggles=audit_toggles,
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

# Non-standard escape sequences some LLMs emit literally in their JSON output.
# Standard JSON escapes (\n, \t, etc.) are already decoded by json.loads;
# these are the ones that slip through as literal two-character sequences.
_LLM_ESCAPE_MAP = [
    ("\\'", "'"),
    ('\\"', '"'),
]


def _unescape_llm_artifacts(text: str) -> str:
    for esc, ch in _LLM_ESCAPE_MAP:
        text = text.replace(esc, ch)
    return text


def _normalize_quotes(text: str) -> str:
    return text.translate(_QUOTE_MAP)


def _strip_outer_asterisks(text: str) -> str:
    """Strip leading/trailing markdown emphasis asterisks (and the whitespace
    just inside them).  Internal asterisks are left untouched."""
    return text.strip().strip("*").strip()


def apply_patches(draft: str, patches: list[dict]) -> tuple[str, list[str]]:
    """Apply search/replace patches to *draft*.  Returns (updated_draft, error_messages)."""
    errors: list[str] = []
    logger.debug("Applying %d patches to draft (%d chars)", len(patches), len(draft))

    for i, p in enumerate(patches):
        search = _unescape_llm_artifacts(p.get("search", ""))
        replace = _unescape_llm_artifacts(p.get("replace", ""))
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

            # Fallback: the model often wraps a single sentence in its own
            # `*...*` when the draft only has block-level asterisks around the
            # whole narration span, so the outer `*` don't line up. Retry with
            # leading/trailing asterisks stripped from both sides.
            trimmed_search = _strip_outer_asterisks(search)
            if trimmed_search and trimmed_search != search:
                trimmed_count = draft.count(trimmed_search)
                if trimmed_count == 1:
                    draft = draft.replace(trimmed_search, _strip_outer_asterisks(replace), 1)
                    logger.debug(
                        "Patch %d OK (asterisk-trimmed): %r → %r",
                        i,
                        trimmed_search[:60],
                        replace[:60],
                    )
                    continue
                elif trimmed_count > 1:
                    errors.append(
                        f"Error: Multiple matches ({trimmed_count}) for {search[:80]!r} (after asterisk trimming). Use more context."
                    )
                    continue

            errors.append(f"Error: {search[:80]!r} not found in draft.")

        elif count > 1:
            errors.append(f"Error: Multiple matches ({count}) for {search[:80]!r}. Use more context.")
        else:
            draft = draft.replace(search, replace, 1)
            logger.debug("Patch %d OK: %r → %r", i, search[:60], replace[:60])

    logger.debug("Patch application done: %d errors out of %d patches", len(errors), len(patches))
    return draft, errors


# ── Editor pass (ReAct loop) ─────────────────────────────────────────────────


def _editor_done_event(
    draft: str | None,
    debug_parts: list[str],
    t0: float,
    tool_calls: list[dict] | None = None,
) -> dict:
    """Build a done event dict for the editor pass."""
    event = {
        "type": "done",
        "draft": draft,
        "debug": "\n---\n".join(debug_parts),
        "elapsed": int((time.monotonic() - t0) * 1000),
    }
    if tool_calls is not None:
        event["tool_calls"] = tool_calls
    return event


async def editor_pass(
    client: LLMClient,
    base: CachedBase,
    effective_msg: str,
    draft: str,
    settings: Mapping[str, Any],
    phrase_bank: list[PhraseGroup],
    audit_enabled: bool = True,
    length_guard: LengthGuard | None = None,
    kv_tracker=None,
    reasoning_on: bool = False,
    audit_context_msgs: list[str] | None = None,
    writer_user_msg: "str | list[ContentPart] | None" = None,
    feedback_fragments: "Sequence[Mapping[str, Any]] | None" = None,
) -> AsyncIterator[dict]:
    """Editor pass = the ReAct edit loop, then an optional feedback sub-step.

    The edit loop fixes audit/length-guard issues in *draft*. Afterwards, if any
    ``field_type='feedback'`` interactive fragments are passed, a feedback step
    runs on the final (edited) text to produce an out-of-character note for the
    user. Feedback is an editor sub-step, not a top-level pass: it shares the
    editor's reasoning toggle (``reasoning_on``), its reasoning channel (deltas
    are tagged ``pass="editor"``), and its ``elapsed`` timing — only the
    user-facing note is surfaced separately. Its single LLM call reuses the
    shared base: give_feedback rides the shared tools blob, and the call replays
    writer_user_msg + reply so it extends the writer/editor KV-cached prefix
    rather than busting it.

    Yields:
        {"type": "reasoning", "delta": str, "pass": "editor"}
        {"type": "done", "draft": str|None, "debug": str, "elapsed": int,
         "tool_calls": list (when the edit loop ran), "feedback": dict}
    """
    t0 = time.monotonic()
    edit_done: dict | None = None
    async for ev in _run_edit_loop(
        client,
        base,
        effective_msg,
        draft,
        settings,
        phrase_bank,
        audit_enabled,
        length_guard,
        kv_tracker=kv_tracker,
        reasoning_on=reasoning_on,
        audit_context_msgs=audit_context_msgs,
        writer_user_msg=writer_user_msg,
    ):
        if ev["type"] == "reasoning":
            yield {"type": "reasoning", "delta": ev["delta"], "pass": "editor"}
        elif ev["type"] == "done":
            edit_done = ev

    # _run_edit_loop yields exactly one done event. A None draft means "unchanged",
    # so the feedback step reads the original text in that case.
    final_text = (edit_done.get("draft") if edit_done else None) or draft

    feedback_values: dict = {}
    if feedback_fragments and final_text and not client.is_aborted:
        async for ev in feedback_step(
            client,
            base,
            final_text,
            settings,
            feedback_fragments,
            # Same value the edit loop replays, so feedback extends the writer's
            # KV-cached prefix instead of forking off the bare base.prefix.
            writer_user_msg=(writer_user_msg if writer_user_msg is not None else effective_msg),
            kv_tracker=kv_tracker,
            # Feedback shares the editor's reasoning toggle — it is a sub-step, not
            # a separately-configurable pass.
            reasoning_on=reasoning_on,
        ):
            if ev["type"] == "reasoning":
                yield {"type": "reasoning", "delta": ev["delta"], "pass": "editor"}
            elif ev["type"] == "done":
                fb: FeedbackResult = ev["result"]
                feedback_values = fb.values

    done = dict(edit_done) if edit_done else {"type": "done", "draft": None, "debug": "", "elapsed": 0}
    done["feedback"] = feedback_values
    # elapsed covers the whole editor pass, feedback sub-step included (the edit
    # loop's own elapsed only timed the loop).
    done["elapsed"] = int((time.monotonic() - t0) * 1000)
    yield done


async def editor_stage(
    cfg: "_PipelineConfig",
    state: "TurnState",
    *,
    settings: Mapping[str, Any],
    phrase_bank: list[PhraseGroup] | None,
    feedback_fragments: Sequence[Mapping[str, Any]],
    editor_audit_msgs: list[str] | None,
    kv_tracker: _KVCacheTracker,
) -> AsyncIterator[dict]:
    """Editor stage: gating + the writer→editor boundary event + editor pass +
    event translation, owned here so the orchestrator only sequences passes.

    Decides whether the editor runs (``cfg.do_edit`` or feedback wanted, given a
    non-empty draft), emits the authoritative ``writer_done`` boundary, then runs
    :func:`editor_pass` translating its ``reasoning`` / ``writer_rewrite`` /
    ``editor_done`` / ``feedback`` events and folding the edit/feedback results
    back into *state* (``resp_text``, ``reasoning_editor``, ``feedback_values``,
    ``latency``).
    """
    # The feedback step is an editor sub-step (post-processing on the final text),
    # not a top-level pass: it shares the editor's reasoning channel and timing and
    # surfaces only its user-facing note. It is gated on the feedback_enabled
    # setting AND at least one enabled feedback-type fragment, so the extra LLM
    # call is fully opt-in. Because feedback is folded in here, we still enter the
    # editor pass (with editing disabled) when only feedback is wanted.
    feedback_needed = _feedback_active(settings, feedback_fragments, agent_on=cfg.agent_on)
    editor_will_run = bool(state.resp_text and (cfg.do_edit or feedback_needed))

    # Authoritative writer→editor boundary. The frontend flips to its "refining"
    # phase on this event (not on a token-gap heuristic, which misfires when slow
    # endpoints stall mid-stream), and only when an editor/feedback pass actually
    # follows. Not emitted on the writer-abort path — afterStream clears the phase
    # there. Mirrors director_start/director_done.
    yield {"event": "writer_done", "data": {"editor_will_run": editor_will_run}}

    if not editor_will_run:
        logger.info(
            "Editor pass skipped (do_edit=%s, feedback=%s, draft=%d chars)",
            cfg.do_edit,
            feedback_needed,
            len(state.resp_text),
        )
        return

    logger.info(
        "Editor pass starting (draft=%d chars, phrase_bank=%d groups, edit=%s, feedback=%s)",
        len(state.resp_text),
        len(phrase_bank) if phrase_bank else 0,
        cfg.do_edit,
        feedback_needed,
    )
    # Errors are not caught here: an editor failure propagates and aborts the
    # turn, like the director/writer passes. _consume_pipeline's finally still
    # fallback-persists whatever the writer already streamed.
    async for event in editor_pass(
        cfg.agent_lane.client,
        cfg.agent_lane.base,
        state.effective_msg,
        state.resp_text,
        settings,
        phrase_bank or [],
        # do_edit == (audit_enabled or length_guard is not None), so in the
        # feedback-only path (do_edit False) both are already inert — pass them
        # straight through and let the edit loop no-op.
        cfg.audit_enabled,
        cfg.length_guard,
        kv_tracker=kv_tracker,
        reasoning_on=cfg.editor_reasoning_on,
        audit_context_msgs=editor_audit_msgs,
        writer_user_msg=state.writer_content,
        feedback_fragments=feedback_fragments if feedback_needed else None,
    ):
        if event["type"] == "reasoning":
            # Feedback reasoning is folded into the editor channel (it is an
            # editor sub-step, so it shares the Editor reasoning toggle and box).
            state.reasoning_editor += event["delta"]
            yield {
                "event": "reasoning",
                "data": {"pass": "editor", "delta": event["delta"]},
            }
        elif event["type"] == "done":
            state.latency += int(event.get("elapsed", 0) or 0)
            refined_draft = event["draft"]
            if refined_draft and refined_draft != state.resp_text:
                state.resp_text = refined_draft
                yield {
                    "event": "writer_rewrite",
                    "data": {"refined_text": state.resp_text},
                }
            if event.get("tool_calls"):
                yield {
                    "event": "editor_done",
                    "data": {"tool_calls": event["tool_calls"]},
                }
            state.feedback_values = event.get("feedback", {}) or {}
            if state.feedback_values:
                yield {
                    "event": "feedback",
                    "data": {"values": state.feedback_values},
                }


async def _run_edit_loop(
    client: LLMClient,
    base: CachedBase,
    effective_msg: str,
    draft: str,
    settings: Mapping[str, Any],
    phrase_bank: list[PhraseGroup],
    audit_enabled: bool = True,
    length_guard: LengthGuard | None = None,
    kv_tracker=None,
    reasoning_on: bool = False,  # If true, use structured tool-use message format (role=tool) for iteration feedback; non-thinking models get a synthetic recap instead
    audit_context_msgs: (
        list[str] | None
    ) = None,  # explicit previous-assistant list for repetition scanning; if None, derived from base.prefix
    writer_user_msg: "str | list[ContentPart] | None" = None,  # writer's exact last user message; when provided replaces bare effective_msg so the editor extends the writer's KV-cached prefix
) -> AsyncIterator[dict]:
    """ReAct-style editor loop with optional audit and/or length guard.

    Yields:
        {"type": "reasoning", "delta": str}                      — reasoning chunks per iteration
        {"type": "done", "draft": str|None, "debug": str, "elapsed": int}
    """
    t0 = time.monotonic()
    debug_parts: list[str] = []

    # Per-scanner on/off map persisted in settings; None falls back to all-on.
    audit_toggles = settings.get("editor_audit_toggles") or None

    # Collect previous assistant messages for cross-message context.
    # audit_context_msgs lets callers override which messages are used, so that
    # super-regenerate doesn't compare the new draft against the message it replaced.
    assistant_messages: list[str] = []
    if audit_enabled:
        if audit_context_msgs is not None:
            assistant_messages = audit_context_msgs[:3]
        else:
            for msg in reversed(base.prefix):
                if msg.get("role") == "assistant":
                    # Assistant history is always plain text; the multimodal
                    # list form only ever rides user messages, so a non-str
                    # body has nothing to contribute to the repetition window.
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        assistant_messages.append(content)
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
        report, report_text = _run_contextual_audit(draft, phrase_bank, assistant_messages, audit_toggles)
        structural_issues = (
            1 if report.structural_repetition_result and report.structural_repetition_result.is_repetitive else 0
        )
        phrase_issues = len(report.phrase_result.flagged_phrases) if report.phrase_result else 0
        logger.info(
            "Editor: initial audit — %d issues (cliches=%d, openers=%d, templates=%d, phrases=%d, structural=%d)",
            report.total_issues,
            report.cliche_result.flagged_count,
            len(report.monotony_result.flagged_openers),
            len(report.template_result.flagged_templates),
            phrase_issues,
            structural_issues,
        )
        debug_parts.append(f"Initial audit ({report.total_issues} issues):\n{report_text}")
    else:
        report = AuditReport.clean()
        report_text = ""
        logger.info("Editor: audit disabled, skipping scanners")

    # ── Length guard
    #
    # The tools blob lives on the shared ``base`` (built once by the orchestrator
    # from the same enabled-tool set as the director and writer). The editor never
    # rebuilds or narrows it: the schemas sit inside the cached prefix, so changing
    # the list mid-loop would bust the KV cache every iteration. Which single tool
    # the model must call is steered entirely by tool_choice (see _pick_tool_choice,
    # recomputed each iteration) while base.tools stays byte-identical throughout.
    length_guard_triggered, length_guard_instruction, lg_word_count = evaluate_length_guard(draft, length_guard)
    if length_guard_triggered and length_guard is not None:  # 2nd clause narrows None for the type checker
        logger.info(
            "Editor: length guard triggered (word_count=%d > max_words=%d)",
            lg_word_count,
            length_guard["max_words"],
        )
        debug_parts.append(f"Length guard triggered: {lg_word_count} words (max {length_guard['max_words']})")

    if report.total_issues <= 1 and not length_guard_triggered:
        logger.info(
            "Editor: %d issue(s) within threshold and no length guard, skipping LLM loop",
            report.total_issues,
        )
        yield _editor_done_event(None, debug_parts, t0)
        return

    if not base.tools:
        logger.info("Editor: no editor tools applicable, skipping LLM loop")
        yield _editor_done_event(None, debug_parts, t0)
        return

    # ── Build message context
    final_prompt = build_editor_prompt(
        audit_enabled and not report.is_clean,
        report_text,
        length_guard_triggered,
        length_guard_instruction,
        structural_rewrite=_structural_rewrite_needed(report),
        reasoning_on=reasoning_on,
    )

    logger.info(final_prompt)

    # base.prefix is the shared, frozen cached bottom; *trailing* is the broader
    # WireMessage buffer the ReAct loop mutates in place (assistant tool_calls,
    # tool-role results) and hands to base.complete() each iteration. Keeping the
    # bottom on the base means the loop can only ever change the top of the stack.
    trailing: list[WireMessage] = [
        {
            "role": "user",
            "content": (writer_user_msg if writer_user_msg is not None else effective_msg),
        },
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
        try:
            reasoning_params = reasoning_cfg(reasoning_on)
            if not reasoning_params["reasoning"].get("enabled", True):
                logger.info("Editor iteration %d: reasoning disabled", iteration + 1)

            logger.debug(
                "Editor iteration %d: sending %d messages to LLM:\n%s",
                iteration + 1,
                len(base.prefix) + len(trailing),
                json.dumps([*base.prefix, *trailing], default=str, indent=2),
            )

            resp: dict = {}
            try:
                hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.25, "max_tokens": 8192})
                async for event in base.complete(
                    client,
                    label="editor",
                    trailing=trailing,
                    tool_choice=_pick_tool_choice(length_guard_triggered, report, audit_enabled),
                    kv_tracker=kv_tracker,
                    **hyperparams,
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
            rewrite_call = next((tc for tc in parsed if tc["name"] == "editor_rewrite"), None)
            if rewrite_call:
                rewritten = rewrite_call.get("arguments", {}).get("rewritten_text", "").strip()
                if not rewritten:
                    logger.info("Editor iteration %d: empty rewrite, stopping", iteration + 1)
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
                debug_parts.append(f"Iteration {iteration + 1}: rewrite applied ({pre_len}→{len(current_draft)} chars)")

                if audit_enabled:
                    report, report_text = _run_contextual_audit(current_draft, phrase_bank, assistant_messages, audit_toggles)
                    debug_parts.append(f"Post-rewrite audit ({report.total_issues} issues):\n{report_text}")
                else:
                    report = AuditReport.clean()
                    report_text = ""

                if report.total_issues <= 1:
                    break
                # Next iteration's tool_choice (via _pick_tool_choice) forces the
                # right tool; base.tools stays the full, byte-identical blob.
                prev_issues = report.total_issues
                if reasoning_on:
                    rewrite_tool_calls = resp.get("tool_calls", [])
                    asst_msg: AssistantToolMessage = {
                        "role": "assistant",
                        "content": resp.get("content") or "",
                        "tool_calls": rewrite_tool_calls,
                    }
                    if resp.get("reasoning_content"):
                        asst_msg["reasoning_content"] = resp["reasoning_content"]
                    trailing.append(asst_msg)
                    if rewrite_tool_calls:
                        trailing.append(
                            {
                                "role": "tool",
                                "tool_call_id": rewrite_tool_calls[0].get("id", ""),
                                "content": report_text,
                            }
                        )
                else:
                    trailing[-2] = {"role": "assistant", "content": current_draft}
                    trailing[-1] = {
                        "role": "user",
                        "content": build_editor_prompt(
                            audit_enabled and not report.is_clean,
                            report_text,
                            length_guard_triggered,
                            length_guard_instruction,
                            structural_rewrite=_structural_rewrite_needed(report),
                            reasoning_on=reasoning_on,
                        ),
                    }
                continue

            # ── Handle editor_apply_patch
            patch_call = next((tc for tc in parsed if tc["name"] == "editor_apply_patch"), None)
            if not patch_call:
                logger.info(
                    "Editor iteration %d: unrecognised tool call, stopping",
                    iteration + 1,
                )
                break

            patches = patch_call.get("arguments", {}).get("patches", [])
            if not patches:
                logger.info("Editor iteration %d: empty patches, stopping", iteration + 1)
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

            report, report_text = _run_contextual_audit(current_draft, phrase_bank, assistant_messages, audit_toggles)
            logger.info(
                "Editor iteration %d: post-audit — %d issues",
                iteration + 1,
                report.total_issues,
            )
            debug_parts.append(f"Post-iteration {iteration + 1} audit ({report.total_issues} issues):\n{report_text}")

            if report.total_issues <= 1:
                if not length_guard_triggered:
                    break
                # Audit clean but length guard still pending: next iteration's
                # tool_choice forces editor_rewrite (length_guard_triggered is
                # still True). The schema blob is left untouched so the KV cache
                # survives the hand-off.
                logger.info("Editor: audit within threshold, length guard still pending — queuing rewrite")

            if report.total_issues >= prev_issues:
                logger.info(
                    "Editor: no progress (%d → %d issues), stopping",
                    prev_issues,
                    report.total_issues,
                )
                break
            prev_issues = report.total_issues

            # Feed results back for next iteration.
            # reasoning_on=True: append structured tool-use/tool-result turns.
            # reasoning_on=False: replace the draft + prompt in-place (same as
            # the rewrite path) so the message list stays flat.
            if reasoning_on:
                _append_iteration_context(trailing, resp, patches, errors, report_text, reasoning_on=True)
            else:
                trailing[-2] = {"role": "assistant", "content": current_draft}
                trailing[-1] = {
                    "role": "user",
                    "content": build_editor_prompt(
                        audit_enabled and not report.is_clean,
                        report_text,
                        length_guard_triggered,
                        length_guard_instruction,
                        structural_rewrite=_structural_rewrite_needed(report),
                        reasoning_on=reasoning_on,
                    ),
                }

        except Exception as e:
            logger.error("Editor iteration %d failed: %s", iteration + 1, e, exc_info=True)
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
    yield _editor_done_event(
        current_draft if changed else None,
        debug_parts,
        t0,
        all_calls,
    )


# ── Helpers (private) ─────────────────────────────────────────────────────────


def _structural_rewrite_needed(report: AuditReport) -> bool:
    return report.structural_repetition_result is not None and report.structural_repetition_result.is_repetitive


def _pick_tool_choice(length_guard_triggered: bool, report: AuditReport, audit_enabled: bool):
    """Determine the tool_choice parameter for the editor LLM call."""
    if length_guard_triggered or _structural_rewrite_needed(report):
        return {"type": "function", "function": {"name": "editor_rewrite"}}
    if audit_enabled:
        return TOOLS["editor_apply_patch"]["choice"]
    return "auto"


def _append_iteration_context(
    msgs: list[WireMessage],
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
        asst_msg: AssistantToolMessage = {
            "role": "assistant",
            "content": resp.get("content") or "",
            "tool_calls": tool_calls,
        }
        if resp.get("reasoning_content"):
            asst_msg["reasoning_content"] = resp["reasoning_content"]
        msgs.append(asst_msg)
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
            "; ".join(f'replaced "{p.get("search", "")[:40]}…"' for p in patches if p.get("search") != p.get("replace"))
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
