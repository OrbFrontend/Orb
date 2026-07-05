"""
editor.py — The editor pass: a ReAct-style loop that fixes audit issues in
the writer's output.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Sequence

from ....analysis import (
    AuditReport,
    DetectionResult,
    format_report,
    run_audit,
)
from .feedback import FeedbackResult, feedback_step

if TYPE_CHECKING:
    from ....database.models import PhraseGroup
    from ...state import TurnState, _PipelineConfig
from ....analysis import (
    FlaggedOpener,
    FlaggedTemplate,
    MonotonyResult,
    TemplateResult,
    split_narration_sentences,
)
from ....core import AssistantToolMessage, ContentPart, WireMessage, extract_hyperparams
from ....inference import (
    TOOLS,
    CachedBase,
    LLMClient,
    _KVCacheTracker,
    build_editor_prompt,
    build_feedback_tool,
    build_patch_target_prompt,
    has_image_parts,
    parse_tool_calls,
    reasoning_cfg,
)
from .length_guard import LengthGuard, evaluate_length_guard

logger = logging.getLogger(__name__)

MAX_EDITOR_ITERATIONS = 3

# How many recent assistant messages the cross-message repetition scanners
# (phrase + structural) compare the draft against.
AUDIT_BASELINE_WINDOW = 20

# Per-iteration cap on prefilled per-finding calls (text mode); the re-audit
# picks up anything beyond the cap on the next iteration.
MAX_PREFILL_TARGETS = 8

# GBNF for the generated remainder of a prefilled editor_apply_patch call: the
# prompt already ends with `{"patches": [{"search": <span>, "replace": "` so
# the model may only emit JSON-string characters plus the exact closing bytes.
# `char` mirrors llama.cpp's own json.gbnf string rule.
_PATCH_REMAINDER_GRAMMAR = r"""root ::= char* "\"}]}"
char ::= [^"\\\x7F\x00-\x1F] | [\\] (["\\bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F])
"""


# ── Feedback gating + tool override ───────────────────────────────────────────


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


# ── Audit-report filtering ────────────────────────────────────────────────────


def _split_target_sentences(target_text: str) -> set[str]:
    """Split *target_text* into a sentence set using the same heuristic as the detectors."""
    return set(split_narration_sentences(target_text))


def _filter_flagged_items(items, sentences: set[str], total: int, *, cls, label_field: str):
    """Filter a list of FlaggedOpener or FlaggedTemplate to sentences in *sentences*.

    Returns a list of *cls* instances with adjusted count and fraction.
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

    Used when the audit ran on concatenated text (draft + previous messages) but
    only issues in the draft itself should be surfaced.
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
    # we keep both unfiltered. Anti-echo is likewise inherently draft-scoped (it
    # only flags questions found in the draft), so it passes through unfiltered.

    return AuditReport(
        cliche_result=filtered_cliche,
        monotony_result=filtered_monotony,
        template_result=filtered_template,
        not_but_result=filtered_not_but,
        phrase_result=report.phrase_result,
        structural_repetition_result=report.structural_repetition_result,
        echo_result=report.echo_result,
    )


# ── Audit with multi-message context ─────────────────────────────────────────


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


def _run_contextual_audit(
    draft: str,
    phrase_bank: list[PhraseGroup],
    previous_assistant_msgs: list[str],
    audit_toggles: dict | None = None,
    user_message: str = "",
) -> tuple[AuditReport, str]:
    """Run the audit on *draft* with cross-message context, filtered to the draft.

    ``user_message`` is the user's immediately-preceding message; the anti-echo
    scanner uses it to flag the draft parroting it back as a question.

    Returns ``(report, report_text)``.
    """
    full_text = _build_audit_text(draft, previous_assistant_msgs)
    # run_audit will append the current text to assistant_messages internally
    raw_report = run_audit(
        full_text,
        phrase_bank,
        assistant_messages=previous_assistant_msgs,
        structural_text=draft,
        user_message=user_message,
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
    """Apply search/replace patches to *draft*. Returns ``(updated_draft, error_messages)``."""
    errors: list[str] = []
    logger.debug("Applying %d patches to draft (%d chars)", len(patches), len(draft))

    for i, p in enumerate(patches):
        # A malformed tool call can place a non-dict where the schema expects a
        # {search, replace} object; skip it rather than crash on .get().
        if not isinstance(p, dict):
            logger.debug("Patch %d: non-dict element (%s), skipping", i, type(p).__name__)
            continue
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
    """Run the ReAct edit loop, then the optional feedback sub-step.

    The edit loop fixes audit and length-guard issues in *draft*. If any
    ``field_type='feedback'`` fragments are provided, a feedback step runs
    on the final text to produce an out-of-character note for the user.
    Feedback shares the editor's reasoning toggle, reasoning channel, and
    ``elapsed`` timing — only the user-facing note is surfaced separately.

    Yields:
        ``{"type": "reasoning", "delta": str, "pass": "editor"}``
        ``{"type": "done", "draft": str|None, "debug": str, "elapsed": int,
         "tool_calls": list, "feedback": dict}``
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
    editor_will_run = bool(state.resp_text and (cfg.do_edit or feedback_needed))

    # Authoritative writer→editor boundary. The frontend flips to its "refining"
    # phase on this event (not on a token-gap heuristic, which misfires when slow
    # endpoints stall mid-stream), and only when an editor/feedback pass actually
    # follows. Not emitted on the writer-abort path — afterStream clears the phase
    # there. Mirrors director_start/director_done.
    yield {"event": "writer_done", "data": {"editor_will_run": editor_will_run}}

    if editor_will_run:
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
    else:
        logger.info(
            "Editor pass skipped (do_edit=%s, feedback=%s, draft=%d chars)",
            cfg.do_edit,
            feedback_needed,
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
    reasoning_on: bool = False,  # If true, use structured tool-use message format (role=tool) for iteration feedback; non-thinking models get a synthetic recap instead
    audit_context_msgs: (
        list[str] | None
    ) = None,  # explicit previous-assistant list for repetition scanning; if None, derived from base.prefix
    writer_user_msg: "str | list[ContentPart] | None" = None,  # writer's exact last user message; when provided replaces bare effective_msg so the editor extends the writer's KV-cached prefix
) -> AsyncIterator[dict]:
    """ReAct-style edit loop with optional audit and/or length guard.

    Yields:
        ``{"type": "reasoning", "delta": str}``
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
        report, report_text = _run_contextual_audit(draft, phrase_bank, assistant_messages, audit_toggles, effective_msg)
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

    # Text-mode endpoints support response prefill → per-finding patch calls
    # (see _collect_prefill_patches). Image-bearing conversations ride the chat
    # transport (which drops prefill), so they keep the classic path. Prefill
    # iterations never extend *trailing*, so the loop pins the flat in-place
    # replay for the whole run — mixing structured appends with flat rewrites
    # would clobber the tail.
    use_prefill = getattr(client, "completion_mode", "chat") == "text" and not has_image_parts([*base.prefix, *trailing])
    replay_structured = reasoning_on and not use_prefill

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
            hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.25, "max_tokens": 8192})
            # Per-finding prefilled calls replace the single big patch call when
            # possible; the rewrite paths (length guard / structural) and the
            # no-unique-span case fall through to the classic call below.
            prefill_targets = (
                _prefill_targets(report, current_draft)
                if use_prefill and audit_enabled and not length_guard_triggered and not _structural_rewrite_needed(report)
                else []
            )

            resp: dict = {}
            if prefill_targets:
                logger.info("Editor iteration %d: prefill mode, %d target(s)", iteration + 1, len(prefill_targets))
                found, prefill_debug = await _collect_prefill_patches(
                    client, base, trailing[0], current_draft, prefill_targets, hyperparams, kv_tracker
                )
                debug_parts.append(f"Iteration {iteration + 1} prefill calls:\n" + "\n".join(prefill_debug))
                # One combined entry — byte-shaped like the classic call's parse,
                # so apply/replay/events downstream stay untouched.
                parsed = [{"name": "editor_apply_patch", "arguments": {"patches": found}}]
            else:
                reasoning_params = reasoning_cfg(reasoning_on)
                if not reasoning_params["reasoning"].get("enabled", True):
                    logger.info("Editor iteration %d: reasoning disabled", iteration + 1)

                logger.debug(
                    "Editor iteration %d: sending %d messages to LLM:\n%s",
                    iteration + 1,
                    len(base.prefix) + len(trailing),
                    json.dumps([*base.prefix, *trailing], default=str, indent=2),
                )

                try:
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
                    report, report_text = _run_contextual_audit(
                        current_draft, phrase_bank, assistant_messages, audit_toggles, effective_msg
                    )
                    debug_parts.append(f"Post-rewrite audit ({report.total_issues} issues):\n{report_text}")
                else:
                    report = AuditReport.clean()
                    report_text = ""

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

            report, report_text = _run_contextual_audit(
                current_draft, phrase_bank, assistant_messages, audit_toggles, effective_msg
            )
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
            # replay_structured: append structured tool-use/tool-result turns.
            # Otherwise (non-thinking models, and always in prefill mode, which
            # never grows the tail): replace the draft + prompt in-place so the
            # message list stays flat.
            if replay_structured:
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
    """Return the ``tool_choice`` value for the editor LLM call."""
    if length_guard_triggered or _structural_rewrite_needed(report):
        return {"type": "function", "function": {"name": "editor_rewrite"}}
    if audit_enabled:
        return TOOLS["editor_apply_patch"]["choice"]
    return "auto"


# ── Text-mode prefill patching ────────────────────────────────────────────────
#
# On a text-completion endpoint the audit already knows every flagged sentence
# byte-exactly, so instead of one big call where the model re-prints each
# `search` string, the loop issues one forced editor_apply_patch call per
# finding with the arguments prefilled up to `"replace": "` — the model
# generates only the replacement, grammar-pinned to a JSON string + the exact
# closing bytes. Kills the wrong/stale-search error class and the tokens spent
# re-printing draft text.


def _patch_prefill(span: str) -> str:
    """The partial editor_apply_patch arguments the transport prefills."""
    return f'{{"patches": [{{"search": {json.dumps(span, ensure_ascii=False)}, "replace": "'


def _prefill_targets(report: AuditReport, draft: str) -> list[tuple[str, str]]:
    """(sentence, why) pairs for the per-finding prefilled patch calls.

    Only spans occurring exactly once in *draft* qualify (apply_patches
    requires a unique match). Repeated openers/templates keep their first
    sentence as the anchor and target the rest. Structural repetition has no
    span — the rewrite path owns it.
    """
    raw: list[tuple[str, str]] = []
    for fs in report.cliche_result.flagged_sentences:
        phrases = ", ".join(f'"{h.phrase}"' for h in fs.cliches)
        raw.append((fs.sentence, f"contains banned phrase(s): {phrases}"))
    for fo in report.monotony_result.flagged_openers:
        for s in fo.sentences[1:]:
            raw.append((s, f'opens with "{fo.opener}" like too many nearby sentences — vary the opening'))
    for ft in report.template_result.flagged_templates:
        for s in ft.sentences[1:]:
            raw.append((s, f'follows the repeated sentence template "{ft.template}" — vary the structure'))
    for nb in report.not_but_result:
        if nb.get("sentence"):
            raw.append((nb["sentence"], "uses the contrastive-negation cliché ('not X, but Y') — rephrase without it"))
    if report.phrase_result:
        for fp in report.phrase_result.flagged_phrases:
            for s in fp.example_sentences:
                if s in draft:
                    raw.append((s, f'reuses the phrase "{fp.phrase}" already seen in {fp.count} previous messages'))
                    break
    if report.echo_result:
        for fe in report.echo_result.flagged_echoes:
            raw.append((fe.echo, "parrots the user's own words back as a question — replace with something new"))

    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for span, why in raw:
        # Flagged sentences keep the narration's outer `*` (format_report strips
        # them only for display). Anchoring the prefilled search on them makes the
        # model's asterisk-free replace eat the paragraph's opening/closing marker,
        # so match on the plain text and leave the `*` wrapping untouched.
        span = _strip_outer_asterisks(span)
        if not span or span in seen or draft.count(span) != 1:
            continue
        seen.add(span)
        targets.append((span, why))
    # Emit in forward (top-to-bottom) document order rather than audit-category
    # order, so the prefilled per-finding calls walk the draft the way the model
    # reads it. Each span is unique (draft.count == 1 above), so index() is exact.
    targets = targets[:MAX_PREFILL_TARGETS]
    targets.sort(key=lambda t: draft.index(t[0]))
    return targets


async def _collect_prefill_patches(
    client: LLMClient,
    base: CachedBase,
    context_user: WireMessage,
    draft: str,
    targets: list[tuple[str, str]],
    hyperparams: dict,
    kv_tracker: _KVCacheTracker | None,
) -> tuple[list[dict], list[str]]:
    """One prefilled forced editor_apply_patch call per flagged sentence.

    Every call extends the same [prefix, user, draft] stack, so only the short
    per-finding tail is uncached. Reasoning is off: the prefilled open turn
    pre-closes the thought channel anyway (and the chat-transport fallback,
    where prefill/grammar are dropped, should answer without thinking too).
    Returns ``(patches, debug_lines)``.
    """
    patches: list[dict] = []
    debug: list[str] = []
    for span, why in targets:
        if client.is_aborted:
            debug.append("aborted mid-batch")
            break
        trailing: list[WireMessage] = [
            context_user,
            {"role": "assistant", "content": draft},
            {"role": "user", "content": build_patch_target_prompt(span, why)},
        ]
        resp: dict = {}
        async for event in base.complete(
            client,
            label="editor",
            trailing=trailing,
            tool_choice=TOOLS["editor_apply_patch"]["choice"],
            kv_tracker=kv_tracker,
            prefill=_patch_prefill(span),
            grammar=_PATCH_REMAINDER_GRAMMAR,
            **hyperparams,
            **reasoning_cfg(False),
        ):
            if event["type"] == "done":
                resp = event["message"]
        got = [
            p
            for call in parse_tool_calls(resp)
            for p in (call.get("arguments") or {}).get("patches", [])
            if isinstance(p, dict) and p.get("search")
        ]
        patches.extend(got)
        debug.append(f"{span[:60]!r} → " + (" / ".join(repr((p.get("replace") or "")[:60]) for p in got) or "<no patch>"))
    return patches, debug


def _append_iteration_context(
    msgs: list[WireMessage],
    resp: dict,
    patches: list[dict],
    errors: list[str],
    report_text: str,
    *,
    reasoning_on: bool,
):
    """Append the assistant recap and tool-result turns for the next iteration.

    ``reasoning_on=True``: structured tool-use format (role=tool) so the model
    sees its exact call and the remaining issues in the form it was trained on.
    ``reasoning_on=False``: a human-readable recap, more reliable for models
    without reasoning.
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
