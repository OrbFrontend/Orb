"""Audit and revise Writer output."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ....analysis import (
    AuditReport,
    Target,
    build_targets,
    format_numbered_report,
    format_report,
    run_audit,
)
from .feedback import FeedbackResult, feedback_step

if TYPE_CHECKING:
    from ....database.models import PhraseGroup
    from ...state import TurnState, _PipelineConfig
# Pure filter/patch helpers live in the analysis layer (analysis/patching.py)
# so non-pipeline consumers (Document mode) can share them; re-imported here
# under their original names so this module's surface is unchanged.
from ....analysis.patching import (
    PatchErrorKind,
    apply_id_patches,
    filter_audit_report_to_text,
)
from ....core import (
    AssistantToolMessage,
    ContentPart,
    WireMessage,
    extract_hyperparams,
    reasoning_delta_event,
)
from ....features.prose_rewriter import ProseRewriteConfig, rewrite_events
from ....inference import (
    CachedBase,
    LLMClient,
    _KVCacheTracker,
    parse_tool_calls,
    reasoning_cfg,
)
from ....prompting.tool_catalog import require_tool
from ....prompting.tool_schemas import build_feedback_tool
from .length_guard import LengthGuard, evaluate_length_guard
from .prompts import EDITOR_RENUMBER_NOTICE, build_editor_prompt

logger = logging.getLogger(__name__)

MAX_EDITOR_ITERATIONS = 3

# How many recent assistant messages the cross-message repetition scanners
# (phrase + structural) compare the draft against.
AUDIT_BASELINE_WINDOW = 20


def _feedback_active(
    settings: Mapping[str, Any],
    feedback_fragments: Sequence[Mapping[str, Any]],
    *,
    agent_on: bool,
) -> bool:
    """Return True when the feedback step should run this turn.

    Requires the feedback flag, the agent on, and at least one enabled feedback
    fragment. *agent_on* is passed in (rather than recomputed) so
    ``agent_enabled`` stays the single source of truth — mirroring
    ``resolve_length_guard``.
    """
    return agent_on and bool(settings.get("feedback_enabled", 0)) and bool(feedback_fragments)


def build_feedback_override(feedback_fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Build the ``give_feedback`` tool schema from *feedback_fragments*.

    Thin wrapper over ``build_feedback_tool`` so ``_build_writer_tools_blob``
    reaches the schema through the editor module rather than importing the
    schema builder directly — symmetric to ``build_direct_scene_override``.
    """
    return build_feedback_tool(feedback_fragments)


def _build_audit_text(draft: str, previous_assistant_msgs: list[str]) -> str:
    """Concatenate previous assistant messages (oldest→newest) with *draft*
    so repetition detectors can see cross-message patterns."""
    if not previous_assistant_msgs:
        return draft
    context = "\n\n".join(reversed(previous_assistant_msgs))
    return context + "\n\n" + draft


def _baseline_window(base: CachedBase, audit_context_msgs: list[str] | None) -> list[str]:
    """The recent assistant-message window (newest first, up to
    AUDIT_BASELINE_WINDOW) the repetition scanners compare the draft against.

    Callers may pass an explicit list via *audit_context_msgs* (e.g.
    super-regenerate, which excludes the message being replaced); when None the
    window is derived from the cached prefix.
    """
    if audit_context_msgs is not None:
        return audit_context_msgs[:AUDIT_BASELINE_WINDOW]
    window: list[str] = []
    for msg in reversed(base.prefix):
        if msg.get("role") == "assistant":
            # Assistant history is always plain text; the multimodal list form
            # only ever rides user messages, so a non-str body has nothing to
            # contribute to the repetition window.
            content = msg.get("content", "")
            if isinstance(content, str):
                window.append(content)
                if len(window) >= AUDIT_BASELINE_WINDOW:
                    break
    return window


async def _run_contextual_audit(
    draft: str,
    phrase_bank: list[PhraseGroup],
    previous_assistant_msgs: list[str],
    audit_toggles: dict | None = None,
    user_message: str = "",
) -> tuple[AuditReport, list[Target]]:
    """Run the audit on *draft* with cross-message context, filtered to the draft.

    ``user_message`` is the user's immediately-preceding message; the anti-echo
    scanner uses it to flag the draft parroting it back as a question.

    Returns ``(report, targets)``. The targets are the ids the model will be
    given, resolved against *this* draft — they go stale the moment the draft
    changes, so every re-audit rebuilds them and they travel with the report
    they were numbered for.
    """
    full_text = _build_audit_text(draft, previous_assistant_msgs)
    # run_audit is CPU-bound; offload it so the single event loop stays free for
    # concurrent requests (e.g. the expression classifier polling during audit).
    raw_report = await asyncio.to_thread(
        run_audit,
        full_text,
        phrase_bank,
        assistant_messages=previous_assistant_msgs,
        structural_text=draft,
        user_message=user_message,
        audit_toggles=audit_toggles,
    )
    filtered = filter_audit_report_to_text(raw_report, draft)
    return filtered, build_targets(filtered, draft)


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
    reasoning_prefill: str = "",
    audit_context_msgs: list[str] | None = None,
    writer_user_msg: str | list[ContentPart] | None = None,
    feedback_fragments: Sequence[Mapping[str, Any]] | None = None,
    prose_rewrite: ProseRewriteConfig | None = None,
) -> AsyncIterator[dict]:
    """Run local rewriting, the audit/edit loop, and feedback."""
    t0 = time.monotonic()
    # The writer's text, kept for the done-event fixup at the bottom: a rewrite
    # the edit loop then found nothing to patch still has to be announced.
    original_draft = draft

    # BEFORE the audit, and that ordering is the whole point: the scanners must
    # see the prose that will actually be persisted, and ``build_targets``
    # anchors byte offsets into the exact string it was handed. Rewriting after
    # the audit would leave every ``Target`` pointing into a draft that no
    # longer exists — "a stale list silently edits the wrong sentences"
    # (``analysis/patching.py``).
    if prose_rewrite is not None and draft:
        async for ev in rewrite_events(draft, prose_rewrite):
            if ev["type"] == "rewritten":
                draft = ev["draft"]  # everything below now reads the rewritten prose
            else:  # draft_update / warning — both travel on as-is
                yield ev

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
        reasoning_prefill=reasoning_prefill,
        audit_context_msgs=audit_context_msgs,
        writer_user_msg=writer_user_msg,
    ):
        if ev["type"] == "reasoning":
            yield {**reasoning_delta_event(ev), "pass": "editor"}
        elif ev["type"] == "draft_update":
            yield ev
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
            reasoning_prefill=reasoning_prefill,
        ):
            if ev["type"] == "reasoning":
                yield {**reasoning_delta_event(ev), "pass": "editor"}
            elif ev["type"] == "done":
                fb: FeedbackResult = ev["result"]
                feedback_values = fb.values

    done = dict(edit_done) if edit_done else {"type": "done", "draft": None, "debug": "", "elapsed": 0}
    # `draft: None` means "unchanged" to editor_stage, which reads it against the
    # WRITER's text. If the local rewriter changed the prose and the edit loop
    # then had nothing to patch, that encoding would silently discard the
    # rewrite — so name the absolute string instead.
    if done.get("draft") is None and final_text != original_draft:
        done["draft"] = final_text
    done["feedback"] = feedback_values
    # elapsed covers the whole editor pass, feedback sub-step included (the edit
    # loop's own elapsed only timed the loop).
    done["elapsed"] = int((time.monotonic() - t0) * 1000)
    yield done


async def editor_stage(
    cfg: _PipelineConfig,
    state: TurnState,
    *,
    settings: Mapping[str, Any],
    phrase_bank: list[PhraseGroup] | None,
    feedback_fragments: Sequence[Mapping[str, Any]],
    editor_audit_msgs: list[str] | None,
    kv_tracker: _KVCacheTracker,
) -> AsyncIterator[dict]:
    """Gating + writer→editor boundary event + editor pass + event translation.

    Decides whether the editor runs (``cfg.do_edit`` or feedback wanted, given a
    non-empty draft), emits the ``writer_done`` boundary, then runs
    :func:`editor_pass` and folds the results back into *state* (``resp_text``,
    ``reasoning_editor``, ``feedback_values``, ``latency``).
    """
    # The feedback step is an editor sub-step (post-processing on the final text),
    # not a top-level pass: it shares the editor's reasoning channel and timing and
    # surfaces only its user-facing note. It is gated on the feedback_enabled
    # setting AND at least one enabled feedback-type fragment, so the extra LLM
    # call is fully opt-in. Because feedback is folded in here, we still enter the
    # editor pass (with editing disabled) when only feedback is wanted.
    feedback_needed = _feedback_active(settings, feedback_fragments, agent_on=cfg.agent_on)
    # The local prose rewriter is a third reason to enter the pass, and it is
    # NOT agent-gated — it runs on its own Local ML toggle. On a rewrite-only
    # turn the edit loop reaches `AuditReport.clean()`, whose total_issues is 0
    # by construction, trips the `<= 1` early exit, and makes no LLM call: the
    # turn costs one local generation and nothing remote.
    editor_will_run = bool(state.resp_text and (cfg.do_edit or feedback_needed or cfg.prose_rewrite is not None))

    # Authoritative writer→editor boundary. The frontend flips to its "refining"
    # phase on this event (not on a token-gap heuristic, which misfires when slow
    # endpoints stall mid-stream), and only when an editor/feedback pass actually
    # follows. Not emitted on the writer-abort path — afterStream clears the phase
    # there. Mirrors director_start/director_done.
    yield {"event": "writer_done", "data": {"editor_will_run": editor_will_run}}

    if editor_will_run:
        logger.info(
            "Editor pass starting (draft=%d chars, phrase_bank=%d groups, edit=%s, feedback=%s, prose_rewrite=%s)",
            len(state.resp_text),
            len(phrase_bank) if phrase_bank else 0,
            cfg.do_edit,
            feedback_needed,
            cfg.prose_rewrite["variant_id"] if cfg.prose_rewrite else None,
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
            reasoning_prefill=cfg.editor_reasoning_prefill,
            audit_context_msgs=editor_audit_msgs,
            writer_user_msg=state.writer_content,
            feedback_fragments=feedback_fragments if feedback_needed else None,
            prose_rewrite=cfg.prose_rewrite,
        ):
            if event["type"] == "reasoning":
                # Feedback reasoning is folded into the editor channel (it is an
                # editor sub-step, so it shares the Editor reasoning toggle and box).
                yield {
                    "event": "reasoning",
                    "data": {"pass": "editor", "delta": state.add_reasoning("editor", event)},
                }
            elif event["type"] == "draft_update":
                # Cosmetic intermediate paint; the done→writer_rewrite block below
                # stays the sole authority over state.resp_text.
                yield {"event": "draft_update", "data": {"draft": event["draft"]}}
            elif event["type"] == "warning":
                # Non-terminal. editor_pass speaks the internal vocabulary and
                # this stage is the only thing that speaks SSE, so the local
                # rewriter's "didn't run" has to be translated here.
                yield {
                    "event": "warning",
                    "data": {
                        "headline": "Prose rewriter didn't run",
                        "sentence": event["reason"],
                        "kind": "local_ml",
                    },
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
    else:
        logger.info(
            "Editor pass skipped (do_edit=%s, feedback=%s, prose_rewrite=%s, draft=%d chars)",
            cfg.do_edit,
            feedback_needed,
            cfg.prose_rewrite is not None,
            len(state.resp_text),
        )


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
    reasoning_prefill: str = "",  # text-mode reasoning prefill, forwarded to reasoning_cfg
    reasoning_on: bool = False,  # If true, use structured tool-use message format (role=tool) for iteration feedback; non-thinking models get a synthetic recap instead
    audit_context_msgs: (
        list[str] | None
    ) = None,  # explicit previous-assistant list for repetition scanning; if None, derived from base.prefix
    writer_user_msg: str
    | list[ContentPart]
    | None = None,  # writer's exact last user message; when provided replaces bare effective_msg so the editor extends the writer's KV-cached prefix
) -> AsyncIterator[dict]:
    """ReAct-style edit loop with optional audit and/or length guard.

    Yields:
        ``{"type": "reasoning", "delta": str}``
        ``{"type": "draft_update", "draft": str}`` — after every mutation of the
        working draft (per applied patch batch/rewrite); cosmetic, the ``done``
        draft stays authoritative
        ``{"type": "done", "draft": str|None, "debug": str, "elapsed": int}``
    """
    t0 = time.monotonic()
    debug_parts: list[str] = []

    # Per-scanner on/off map persisted in settings; None falls back to all-on.
    audit_toggles = settings.get("editor_audit_toggles") or None

    # Collect previous assistant messages for cross-message context.
    # audit_context_msgs lets callers override which messages are used, so that
    # super-regenerate doesn't compare the new draft against the message it replaced.
    assistant_messages: list[str] = _baseline_window(base, audit_context_msgs) if audit_enabled else []

    # ── Initial audit
    if audit_enabled:
        logger.info(
            "Editor: audit on draft (%d chars), %d previous messages, %d phrase groups",
            len(draft),
            len(assistant_messages),
            len(phrase_bank),
        )
        report, targets = await _run_contextual_audit(draft, phrase_bank, assistant_messages, audit_toggles, effective_msg)
        structural_issues = (
            1 if report.structural_repetition_result and report.structural_repetition_result.is_repetitive else 0
        )
        phrase_issues = len(report.phrase_result.flagged_phrases) if report.phrase_result else 0
        logger.info(
            "Editor: initial audit — %d issues (cliches=%d, openers=%d, templates=%d, phrases=%d, structural=%d) → %d target(s)",
            report.total_issues,
            report.cliche_result.flagged_count,
            len(report.monotony_result.flagged_openers),
            len(report.template_result.flagged_templates),
            phrase_issues,
            structural_issues,
            len(targets),
        )
    else:
        report = AuditReport.clean()
        targets = []
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
    if audit_enabled:
        debug_parts.append(
            f"Initial audit ({report.total_issues} issues):\n"
            + _render_report(report, targets, length_guard_triggered=length_guard_triggered)
        )
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
    final_prompt, report_text = _build_editor_request(
        report,
        targets,
        audit_enabled=audit_enabled,
        length_guard_triggered=length_guard_triggered,
        length_guard_instruction=length_guard_instruction,
        reasoning_on=reasoning_on,
    )

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

    replay_structured = reasoning_on

    current_draft = draft
    prev_issues = report.total_issues
    all_calls: list[dict] = []
    # At most one extra iteration per pass is spent explaining a guard
    # rejection; see where it is set.
    guard_retry_spent = False

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
            hyperparams = extract_hyperparams(settings, lane="agent", token_floor=8192, defaults={"temperature": 0.25})
            reasoning_params = reasoning_cfg(reasoning_on, reasoning_prefill)
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
                async for event in base.complete_into(
                    client,
                    resp,
                    label="editor",
                    trailing=trailing,
                    tool_choice=_pick_tool_choice(length_guard_triggered, report, audit_enabled, targets),
                    kv_tracker=kv_tracker,
                    **hyperparams,
                    **reasoning_params,
                ):
                    yield event
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
                # An explicit ``"rewritten_text": null`` is the model declining the
                # forced call, and reads the same as the empty string the break
                # below already handles -- the default only covers an absent key,
                # so coerce before .strip() rather than after.
                raw_rewrite = rewrite_call.get("arguments", {}).get("rewritten_text")
                rewritten = raw_rewrite.strip() if isinstance(raw_rewrite, str) else ""
                if not rewritten:
                    logger.info("Editor iteration %d: empty rewrite, stopping", iteration + 1)
                    break
                pre_len = len(current_draft)
                current_draft = rewritten
                yield {"type": "draft_update", "draft": current_draft}
                length_guard_triggered = False
                logger.info(
                    "Editor iteration %d: rewrite applied, draft %d→%d chars",
                    iteration + 1,
                    pre_len,
                    len(current_draft),
                )
                debug_parts.append(f"Iteration {iteration + 1}: rewrite applied ({pre_len}→{len(current_draft)} chars)")

                if audit_enabled:
                    report, targets = await _run_contextual_audit(
                        current_draft, phrase_bank, assistant_messages, audit_toggles, effective_msg
                    )
                else:
                    report = AuditReport.clean()
                    targets = []
                next_prompt, report_text = _build_editor_request(
                    report,
                    targets,
                    audit_enabled=audit_enabled,
                    length_guard_triggered=length_guard_triggered,
                    length_guard_instruction=length_guard_instruction,
                    reasoning_on=reasoning_on,
                )
                if audit_enabled:
                    debug_parts.append(f"Post-rewrite audit ({report.total_issues} issues):\n{report_text}")

                if report.total_issues <= 1:
                    break
                # Next iteration's tool_choice (via _pick_tool_choice) forces the
                # right tool; base.tools stays the full, byte-identical blob.
                prev_issues = report.total_issues
                if replay_structured:
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
                                "content": _tool_result_text([], report_text, renumbered=bool(targets)),
                            }
                        )
                else:
                    trailing[-2] = {"role": "assistant", "content": current_draft}
                    trailing[-1] = {"role": "user", "content": next_prompt}
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
            # `targets` are the ids that numbered the report this call answered;
            # the re-audit below replaces them for the next iteration.
            current_draft, errors = apply_id_patches(current_draft, targets, patches)
            yield {"type": "draft_update", "draft": current_draft}
            logger.info(
                "Editor iteration %d: applied %d patches, draft %d→%d chars",
                iteration + 1,
                len(patches),
                pre_len,
                len(current_draft),
            )
            for e in errors:
                logger.warning("Editor iteration %d patch error: %s", iteration + 1, e)
            rejected = [e for e in errors if e.kind == PatchErrorKind.PROTECTED_SEQUENCE]

            report, targets = await _run_contextual_audit(
                current_draft, phrase_bank, assistant_messages, audit_toggles, effective_msg
            )
            next_prompt, report_text = _build_editor_request(
                report,
                targets,
                audit_enabled=audit_enabled,
                length_guard_triggered=length_guard_triggered,
                length_guard_instruction=length_guard_instruction,
                reasoning_on=reasoning_on,
            )
            logger.info(
                "Editor iteration %d: post-audit — %d issues",
                iteration + 1,
                report.total_issues,
            )
            debug_parts.append(f"Post-iteration {iteration + 1} audit ({report.total_issues} issues):\n{report_text}")

            # ── Explaining a protected-sequence rejection
            #
            # A guard rejection is the one failure the model could act on and
            # never hears about. The rejected target keeps the writer's original
            # text, so its issues cannot clear, so `total_issues` cannot improve
            # — which means one of the two stops below *always* fires first, and
            # the replay that would have carried the reason is never reached.
            # The model is left believing its patch landed.
            #
            # On the structured path the reason has somewhere to go: the
            # tool-result turn already carries `errors` verbatim. So keep the
            # loop alive for exactly one more iteration to deliver it, once per
            # pass, and only while there is still a target to redo. The flat
            # recap non-thinking models get has no tool-result slot, so those
            # keep the quieter behaviour of stopping and leaving the span as the
            # writer wrote it.
            explain_rejection = bool(rejected) and replay_structured and not guard_retry_spent and bool(targets)
            if explain_rejection:
                guard_retry_spent = True
                logger.info(
                    "Editor iteration %d: %d patch(es) rejected by the protected-sequence guard, "
                    "continuing once to tell the model why",
                    iteration + 1,
                    len(rejected),
                )
                debug_parts.append("Protected-sequence rejection replayed to the model:\n" + "\n".join(rejected))

            if report.total_issues <= 1 and not explain_rejection:
                if not length_guard_triggered:
                    break
                # Audit clean but length guard still pending: next iteration's
                # tool_choice forces editor_rewrite (length_guard_triggered is
                # still True). The schema blob is left untouched so the KV cache
                # survives the hand-off.
                logger.info("Editor: audit within threshold, length guard still pending — queuing rewrite")

            if report.total_issues >= prev_issues and not explain_rejection:
                logger.info(
                    "Editor: no progress (%d → %d issues), stopping",
                    prev_issues,
                    report.total_issues,
                )
                break
            prev_issues = report.total_issues

            # Feed results back for next iteration.
            # replay_structured: append structured tool-use/tool-result turns.
            # Otherwise (non-thinking models): replace the draft + prompt in-place
            # so the message list stays flat.
            if replay_structured:
                _append_iteration_context(trailing, resp, errors, report_text, renumbered=bool(targets))
            else:
                trailing[-2] = {"role": "assistant", "content": current_draft}
                trailing[-1] = {"role": "user", "content": next_prompt}

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


def _structural_rewrite_needed(report: AuditReport) -> bool:
    return report.structural_repetition_result is not None and report.structural_repetition_result.is_repetitive


def _pick_tool_choice(length_guard_triggered: bool, report: AuditReport, audit_enabled: bool, targets: Sequence[Target]):
    """Return the ``tool_choice`` value for the editor LLM call.

    Patching needs ids to address, so a non-clean report that resolved to no
    targets forces the rewrite instead — the same condition
    :func:`_build_editor_request` renders the sectioned report for.
    """
    if length_guard_triggered or _rewrite_only(report, targets):
        return {"type": "function", "function": {"name": "editor_rewrite"}}
    if audit_enabled:
        return require_tool("editor_apply_patch")["choice"]
    return "auto"


def _rewrite_only(report: AuditReport, targets: Sequence[Target]) -> bool:
    """Whether the findings can only be fixed by a whole-draft rewrite.

    Either structural repetition (which has no span at all), or an audit that
    flagged something none of whose spans could be located in the draft — the
    detectors do not all segment identically, so a finding can survive filtering
    and still not resolve to an offset. Left to the patch path that case renders
    as "all checks passed" while ``tool_choice`` forces a patch call against an
    empty id set.
    """
    return _structural_rewrite_needed(report) or (not report.is_clean and not targets)


def _render_report(report: AuditReport, targets: Sequence[Target], *, length_guard_triggered: bool) -> str:
    """The report text the model sees this iteration.

    Numbered when the call will patch, sectioned when it will rewrite: the
    rewrite tool takes whole text and has no ids to address, and a flat numbered
    list cannot carry the structural-repetition finding at all.
    """
    if length_guard_triggered or _rewrite_only(report, targets):
        return format_report(report)
    return format_numbered_report(targets)


def _build_editor_request(
    report: AuditReport,
    targets: Sequence[Target],
    *,
    audit_enabled: bool,
    length_guard_triggered: bool,
    length_guard_instruction: str,
    reasoning_on: bool,
) -> tuple[str, str]:
    """``(prompt, report_text)`` for one editor iteration, rendered in lockstep.

    Kept as one call because the prompt's patch/rewrite branch and the report's
    numbered/sectioned rendering must agree: a numbered report beside rewrite
    instructions offers ids no tool can take, and a sectioned report beside
    patch instructions offers no ids at all.
    """
    report_text = _render_report(report, targets, length_guard_triggered=length_guard_triggered)
    prompt = build_editor_prompt(
        audit_enabled and not report.is_clean,
        report_text,
        length_guard_triggered,
        length_guard_instruction,
        structural_rewrite=_structural_rewrite_needed(report),
        reasoning_on=reasoning_on,
        patchable=bool(targets),
    )
    return prompt, report_text


def _tool_result_text(errors: Sequence[str], report_text: str, *, renumbered: bool) -> str:
    """The tool-result content fed back on the structured-replay path.

    Apply errors first (they name the ids the model just used), then the
    renumbering notice when the report below carries fresh ids, then the report.
    """
    parts = []
    if errors:
        parts.append("\n".join(errors))
    if renumbered:
        parts.append(EDITOR_RENUMBER_NOTICE)
    parts.append(report_text)
    return "\n\n".join(parts)


def _append_iteration_context(
    msgs: list[WireMessage],
    resp: dict,
    errors: Sequence[str],
    report_text: str,
    *,
    renumbered: bool,
):
    """Append the assistant tool-call recap + tool-result turn for the next
    iteration, in structured tool-use format (role=tool) so the model sees its
    exact call and the remaining issues in the form it was trained on. Only the
    reasoning/structured-replay path reaches this; non-reasoning modes re-send
    the updated draft in place instead.

    This is the one replay that shows the model its old ids next to a fresh
    report, so *renumbered* makes the id lifecycle explicit rather than leaving
    it to be inferred — see EDITOR_RENUMBER_NOTICE.
    """
    tool_response = _tool_result_text(errors, report_text, renumbered=renumbered)
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
