"""Audit and revise Writer output."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple

from ....analysis import AuditReport, Target, build_targets, format_numbered_report, format_report, run_audit
from ....core.settings import Settings
from ...events import CoreTurnEvent
from ...failures import STAGE_EDITOR, step_failure_warning
from ..judge import JudgeConfig
from .feedback import FeedbackResult, feedback_step
from .post_processing import PostProcessingResult, post_processing_active, post_processing_step

if TYPE_CHECKING:
    from ....database.models import PhraseGroup
    from ...state import PipelineConfig, TurnState
# Pure filter/patch helpers live in the analysis layer (analysis/patching.py) so non-pipeline consumers (Document mode) can
# share them; re-imported here under their original names so this module's surface is unchanged.
from ....analysis.patching import PatchError, PatchErrorKind, apply_id_patches, filter_audit_report_to_text
from ....core import AssistantToolMessage, ContentPart, WireMessage, extract_hyperparams, reasoning_delta_event
from ....core.llm_types import CompletionMessage, ParsedToolCall
from ....inference import CachedBase, KVCacheTracker, LLMClient, parse_tool_calls, reasoning_cfg, replay_reasoning
from ....prompting.tool_catalog import require_tool
from ....prompting.tool_schemas import build_feedback_tool
from .length_guard import LengthGuard, evaluate_length_guard
from .prompts import EDITOR_RENUMBER_NOTICE, build_editor_prompt, editor_patches, patch_instructions

logger = logging.getLogger(__name__)

MAX_EDITOR_ITERATIONS = 3

# How many recent assistant messages the cross-message repetition scanners (phrase + structural) compare the draft against.
AUDIT_BASELINE_WINDOW = 20


def feedback_active(feedback_fragments: Sequence[Mapping[str, Any]], *, agent_on: bool) -> bool:
    """Return True when the feedback step should run this turn.

    Requires the agent on and at least one enabled feedback fragment, like ``post_processing_active``. *agent_on* is passed in
    (rather than recomputed) so ``agent_enabled`` stays the single source of truth.
    """
    return agent_on and bool(feedback_fragments)


def build_feedback_override(feedback_fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Build the ``give_feedback`` tool schema from *feedback_fragments*.

    Thin wrapper over ``build_feedback_tool`` so ``build_writer_tools_blob`` reaches the schema through the editor module rather
    than importing the schema builder directly -- symmetric to ``build_direct_scene_override``.
    """
    return build_feedback_tool(feedback_fragments)


def _build_audit_text(draft: str, previous_assistant_msgs: list[str]) -> str:
    """Concatenate previous assistant messages (oldest->newest) with *draft*
    so repetition detectors can see cross-message patterns."""
    if not previous_assistant_msgs:
        return draft
    context = "\n\n".join(reversed(previous_assistant_msgs))
    return context + "\n\n" + draft


def _baseline_window(base: CachedBase, audit_context_msgs: list[str] | None) -> list[str]:
    """The recent assistant-message window (newest first, up to AUDIT_BASELINE_WINDOW) the repetition scanners compare the draft
    against and post-processing gates draw their previous replies from.

    Callers may pass an explicit list via *audit_context_msgs* (e.g. super-regenerate, which excludes the message being
    replaced); when None the window is derived from the cached prefix.
    """
    if audit_context_msgs is not None:
        return audit_context_msgs[:AUDIT_BASELINE_WINDOW]
    window: list[str] = []
    for msg in reversed(base.prefix):
        if msg.get("role") == "assistant":
            # Assistant history is always plain text; the multimodal list form only ever rides user messages, so a non-str body
            # has nothing to contribute to the repetition window.
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

    ``user_message`` is the user's immediately-preceding message; the anti-echo scanner uses it to flag the draft parroting it
    back as a question.

    Returns ``(report, targets)``. The targets are the ids the model will be given, resolved against *this* draft -- they go
    stale the moment the draft changes, so every re-audit rebuilds them and they travel with the report they were numbered for.
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
    draft: str | None, debug_parts: list[str], t0: float, tool_calls: list[ParsedToolCall] | None = None
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
    settings: Settings,
    phrase_bank: list[PhraseGroup],
    audit_enabled: bool = True,
    length_guard: LengthGuard | None = None,
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
    audit_context_msgs: list[str] | None = None,
    writer_user_msg: str | list[ContentPart] | None = None,
    feedback_fragments: Sequence[Mapping[str, Any]] | None = None,
    post_processing_fragments: Sequence[Mapping[str, Any]] | None = None,
    judge_config: JudgeConfig | None = None,
) -> AsyncIterator[Mapping[str, Any]]:
    """Run the audit/edit loop, post-processing fragments, and feedback.

    A failing call does not end the pass. Its sub-step reports it as a ``failure`` event and keeps what its finished calls
    produced; the later sub-steps still run on the best draft reached.
    """
    t0 = time.monotonic()
    # Every sub-step replays the Writer's exact last message, so each extends the writer's KV-cached prefix instead of forking
    # off the bare base.prefix. They also share the editor's reasoning toggle: they are sub-steps, not separate passes.
    writer_msg = writer_user_msg if writer_user_msg is not None else effective_msg

    if audit_enabled:
        yield {"type": "step", "step": "output_auditor"}
    elif length_guard is not None:
        yield {"type": "step", "step": "length_guard"}
    done: dict = {"type": "done", "draft": None, "debug": "", "elapsed": 0}
    async for ev in _editor_events(
        _run_edit_loop(
            client,
            base,
            effective_msg,
            draft,
            settings,
            phrase_bank,
            audit_enabled,
            length_guard,
            writer_user_msg=writer_msg,
            kv_tracker=kv_tracker,
            reasoning_on=reasoning_on,
            reasoning_prefill=reasoning_prefill,
            audit_context_msgs=audit_context_msgs,
        )
    ):
        if ev["type"] == "done":
            done = dict(ev)
        else:
            yield ev

    # A None draft means "unchanged"; an empty string remains a meaningful post-processing result.
    final_text = draft if done["draft"] is None else done["draft"]

    if post_processing_fragments and not client.is_aborted:
        yield {"type": "step", "step": "post_processing"}
        async for ev in _editor_events(
            post_processing_step(
                client,
                base,
                final_text,
                settings,
                post_processing_fragments,
                writer_user_msg=writer_msg,
                effective_msg=effective_msg,
                judge_config=judge_config,
                recent_replies=_baseline_window(base, audit_context_msgs),
                kv_tracker=kv_tracker,
                reasoning_on=reasoning_on,
                reasoning_prefill=reasoning_prefill,
            )
        ):
            if ev["type"] == "done":
                post: PostProcessingResult = ev["result"]
                final_text = post.draft
                if post.tool_calls:
                    done["tool_calls"] = [*(done.get("tool_calls") or []), *post.tool_calls]
            else:
                yield ev

    done["feedback"] = {}
    if feedback_fragments and final_text and not client.is_aborted:
        yield {"type": "step", "step": "feedback"}
        async for ev in _editor_events(
            _reporting_failures(
                feedback_step(
                    client,
                    base,
                    final_text,
                    settings,
                    feedback_fragments,
                    writer_user_msg=writer_msg,
                    kv_tracker=kv_tracker,
                    reasoning_on=reasoning_on,
                    reasoning_prefill=reasoning_prefill,
                ),
                during="feedback",
                note="Feedback step failed; the reply keeps no feedback",
            )
        ):
            if ev["type"] == "done":
                fb: FeedbackResult = ev["result"]
                done["feedback"] = fb.values
            else:
                yield ev

    done["draft"] = final_text if final_text != draft else None
    # elapsed covers the whole editor pass, feedback sub-step included (the edit loop's own elapsed only timed the loop).
    done["elapsed"] = int((time.monotonic() - t0) * 1000)
    yield done


async def _editor_events(events: AsyncIterator[Mapping[str, Any]]) -> AsyncIterator[Mapping[str, Any]]:
    """A sub-step's events as the editor pass relays them: reasoning on the editor channel, edits, failures, and its ``done``."""
    async for ev in events:
        if ev["type"] == "reasoning":
            yield {**reasoning_delta_event(ev), "pass": "editor"}
        elif ev["type"] in ("draft_update", "failure", "done"):
            yield ev


async def _reporting_failures(
    events: AsyncIterator[Mapping[str, Any]], *, during: str, note: str
) -> AsyncIterator[Mapping[str, Any]]:
    """Pass *events* through, turning a failure that escapes them into a ``failure`` event *during* that step, logged with
    *note*. The draft stays what the last ``done`` or ``draft_update`` made it."""
    try:
        async for event in events:
            yield event
    except Exception as exc:
        logger.exception(note)
        yield {"type": "failure", "during": during, "label": "", "error": exc}


async def editor_stage(
    cfg: PipelineConfig,
    state: TurnState,
    *,
    settings: Settings,
    phrase_bank: list[PhraseGroup] | None,
    feedback_fragments: Sequence[Mapping[str, Any]],
    post_processing_fragments: Sequence[Mapping[str, Any]] = (),
    editor_audit_msgs: list[str] | None,
    kv_tracker: KVCacheTracker,
    judge_config: JudgeConfig | None = None,
) -> AsyncIterator[CoreTurnEvent]:
    """Gating + writer->editor boundary event + editor pass + event translation.

    Decides whether the editor runs (``cfg.do_edit`` or feedback wanted, given a non-empty draft), emits the ``writer_done``
    boundary, then runs :func:`editor_pass` and folds the results back into *state* (``resp_text``, ``reasoning_editor``,
    ``feedback_values``, ``latency``).
    """
    # The feedback step is an editor sub-step (post-processing on the final text), not a top-level pass: it shares the editor's
    # reasoning channel and timing and surfaces only its user-facing note. It is gated on the Agent AND at least one enabled
    # feedback-type fragment, so the extra LLM call is fully opt-in. Because feedback is folded in here, we still enter the
    # editor pass (with audit/guard editing disabled) when only fragment work is wanted.
    feedback_needed = feedback_active(feedback_fragments, agent_on=cfg.agent_on)
    post_processing_needed = post_processing_active(post_processing_fragments, agent_on=cfg.agent_on)
    editor_will_run = bool(state.resp_text and (cfg.do_edit or post_processing_needed or feedback_needed))

    # writer_done says whether an Editor sub-step follows; each sub-step emits its own step_start.
    yield {"event": "writer_done", "data": {"editor_will_run": editor_will_run}}

    if editor_will_run:
        logger.info(
            "Editor pass starting (draft=%d chars, phrase_bank=%d groups, edit=%s, post_processing=%s, feedback=%s)",
            len(state.resp_text),
            len(phrase_bank) if phrase_bank else 0,
            cfg.do_edit,
            post_processing_needed,
            feedback_needed,
        )
        # The draft the browser was last told is authoritative.
        announced = state.resp_text
        # A failed Editor call does not abort the turn: editor_pass keeps the best draft reached and reports the failure as a
        # non-terminal ``warning``. A failure outside the sub-steps' own reporting, such as the initial audit, lands here.
        async for event in _reporting_failures(
            editor_pass(
                cfg.agent_lane.client,
                cfg.agent_lane.base,
                state.effective_msg,
                state.resp_text,
                settings,
                phrase_bank or [],
                # do_edit == (audit_enabled or length_guard is not None), so in the feedback-only path (do_edit False) both are
                # already inert -- pass them straight through and let the edit loop no-op.
                cfg.audit_enabled,
                cfg.length_guard,
                kv_tracker=kv_tracker,
                reasoning_on=cfg.editor_reasoning_on,
                reasoning_prefill=cfg.editor_reasoning_prefill,
                audit_context_msgs=editor_audit_msgs,
                writer_user_msg=state.writer_content,
                post_processing_fragments=post_processing_fragments if post_processing_needed else None,
                feedback_fragments=feedback_fragments if feedback_needed else None,
                judge_config=judge_config,
            ),
            during="editor",
            note="Editor pass failed; keeping the draft it had reached",
        ):
            if event["type"] == "step":
                yield {"event": "step_start", "data": {"step": event["step"]}}
            elif event["type"] == "failure":
                yield step_failure_warning(event["error"], event["during"], stage=STAGE_EDITOR, label=event.get("label", ""))
            elif event["type"] == "reasoning":
                # Feedback reasoning is folded into the editor channel (it is an
                # editor sub-step, so it shares the Editor reasoning toggle and box).
                yield {"event": "reasoning", "data": {"pass": "editor", "delta": state.add_reasoning("editor", event)}}
            elif event["type"] == "draft_update":
                # Each one is a finished patch batch, rewrite, or fragment edit, so the turn keeps it if it is stopped before
                # ``done``. To the browser it stays a cosmetic paint: ``writer_rewrite`` below is still the one announcement of
                # the Editor's result.
                state.resp_text = event["draft"]
                yield {"event": "draft_update", "data": {"draft": event["draft"]}}
            elif event["type"] == "done":
                state.latency += int(event.get("elapsed", 0) or 0)
                refined_draft = event["draft"]
                state.resp_text = announced if refined_draft is None else refined_draft
                if state.resp_text != announced:
                    announced = state.resp_text
                    yield {"event": "writer_rewrite", "data": {"refined_text": state.resp_text}}
                if event.get("tool_calls"):
                    # On state.calls too, so the saved log lists them as the live Inspector does after merging ``editor_done``.
                    state.calls = [*state.calls, *event["tool_calls"]]
                    yield {"event": "editor_done", "data": {"tool_calls": event["tool_calls"]}}
                state.feedback_values = event.get("feedback", {}) or {}
                if state.feedback_values:
                    yield {"event": "feedback", "data": {"values": state.feedback_values}}
        # A pass that failed outside its sub-steps ends without ``done``; the
        # draft it had reached is still the reply, so announce it.
        if state.resp_text != announced:
            yield {"event": "writer_rewrite", "data": {"refined_text": state.resp_text}}
    else:
        logger.info(
            "Editor pass skipped (do_edit=%s, post_processing=%s, feedback=%s, draft=%d chars)",
            cfg.do_edit,
            post_processing_needed,
            feedback_needed,
            len(state.resp_text),
        )


async def _run_edit_loop(
    client: LLMClient,
    base: CachedBase,
    effective_msg: str,
    draft: str,
    settings: Settings,
    phrase_bank: list[PhraseGroup],
    audit_enabled: bool,
    length_guard: LengthGuard | None,
    *,
    writer_user_msg: str | list[ContentPart],
    kv_tracker: KVCacheTracker | None = None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
    audit_context_msgs: list[str] | None = None,
) -> AsyncIterator[Mapping[str, Any]]:
    """Run the edit loop with optional audit and length guard.

    Yield reasoning, whole draft_update snapshots after mutations, failure on a failed iteration, then done with the final
    draft/debug/elapsed. Stopped turns retain completed draft updates. *writer_user_msg* is the Writer's exact last user message,
    replayed so the editor extends the Writer's KV-cached prefix.
    """
    t0 = time.monotonic()

    # The tools blob lives on the shared ``base`` (built once by the orchestrator from the same enabled-tool set as the director
    # and writer). The editor never rebuilds or narrows it: the schemas sit inside the cached prefix, so changing the list
    # mid-loop would bust the KV cache every iteration. Which single tool the model must call is steered entirely by tool_choice
    # (see _pick_tool_choice, recomputed each iteration) while base.tools stays byte-identical throughout. A forced call can
    # only name a tool that blob already carries, so a rewrite needs ``editor_rewrite`` there. The blob offers it whenever the
    # Agent is on; the length guard is what opts a turn into whole-draft rewrites.
    can_rewrite = length_guard is not None and any(schema["function"]["name"] == "editor_rewrite" for schema in base.tools)
    lg_triggered, lg_instruction, lg_word_count = evaluate_length_guard(draft, length_guard)
    if lg_triggered and not can_rewrite:
        logger.warning("Editor: length guard triggered, but the tools blob has no editor_rewrite to force; skipping it")
        lg_triggered = False

    loop = _EditLoop(
        client=client,
        base=base,
        phrase_bank=phrase_bank,
        effective_msg=effective_msg,
        draft=draft,
        audit_enabled=audit_enabled,
        reasoning_on=reasoning_on,
        params={**extract_hyperparams(settings, lane="agent"), **reasoning_cfg(reasoning_on, reasoning_prefill)},
        kv_tracker=kv_tracker,
        audit_toggles=settings.get("editor_audit_toggles") or None,
        # audit_context_msgs lets callers override which messages are used, so that super-regenerate doesn't compare the new
        # draft against the message it replaced.
        assistant_messages=_baseline_window(base, audit_context_msgs) if audit_enabled else [],
        can_rewrite=can_rewrite,
        length_guard_triggered=lg_triggered,
        length_guard_instruction=lg_instruction,
    )
    await loop.initial_audit()
    if lg_triggered and length_guard is not None:  # 2nd clause narrows None for the type checker
        logger.info("Editor: length guard triggered (word_count=%d > max_words=%d)", lg_word_count, length_guard["max_words"])
        loop.debug_parts.append(f"Length guard triggered: {lg_word_count} words (max {length_guard['max_words']})")

    if skip := loop.skip_reason():
        logger.info("Editor: %s, skipping LLM loop", skip)
        yield _editor_done_event(None, loop.debug_parts, t0)
        return

    loop.start(writer_user_msg)
    async for event in loop.run():
        yield event

    elapsed = int((time.monotonic() - t0) * 1000)
    changed = loop.draft != draft
    logger.info("Editor: done in %dms, changed=%s, final_draft=%d chars", elapsed, changed, len(loop.draft))
    yield _editor_done_event(loop.draft if changed else None, loop.debug_parts, t0, loop.calls)


class _Request(NamedTuple):
    """One editor call's prompt and the report it carries; see :func:`_build_editor_request`."""

    prompt: str
    report_text: str
    # The audit categories whose patching rules the prompt carries, or None for a rewrite request.
    ruled: frozenset[str] | None


@dataclass(slots=True)
class _EditLoop:
    """The edit loop's inputs and what it carries from one iteration to the next.

    *report*, *targets*, *rewrite*, and *request* always describe the current *draft*: every edit re-audits, so the ids the
    next call is given number the draft it will patch. *trailing* is the WireMessage buffer the loop mutates in place
    (assistant tool_calls, tool-role results) and hands to ``base.complete_into`` each iteration; ``base.prefix`` stays the
    shared, frozen cached bottom, so the loop can only ever change the top of the stack.
    """

    client: LLMClient
    base: CachedBase
    phrase_bank: list[PhraseGroup]
    effective_msg: str
    draft: str
    audit_enabled: bool
    # Thinking models get each iteration replayed as structured tool-use/tool-result turns (role=tool); non-thinking models a
    # flat recap that swaps the draft and request in place.
    reasoning_on: bool
    # Sampling and reasoning parameters, the same for every call.
    params: dict[str, Any]
    kv_tracker: KVCacheTracker | None
    # Per-scanner on/off map persisted in settings; None falls back to all-on.
    audit_toggles: dict | None
    assistant_messages: list[str]
    can_rewrite: bool
    length_guard_triggered: bool
    length_guard_instruction: str
    report: AuditReport = field(default_factory=AuditReport.clean)
    targets: list[Target] = field(default_factory=list)
    rewrite: bool = False
    request: _Request = _Request("", "", None)
    trailing: list[WireMessage] = field(default_factory=list)
    # The patching rules the conversation carries, for the structured replay, whose tool results must add any a later report
    # needs; None until a patch request has been sent.
    rules_shown: set[str] | None = None
    prev_issues: int = 0
    calls: list[ParsedToolCall] = field(default_factory=list)
    # At most one extra iteration per pass is spent explaining a guard rejection; see _explain_rejection.
    guard_retry_spent: bool = False
    debug_parts: list[str] = field(default_factory=list)

    async def audit(self) -> None:
        """Audit the draft (a clean report when auditing is off), then re-decide the rewrite and render the next request."""
        if self.audit_enabled:
            self.report, self.targets = await _run_contextual_audit(
                self.draft, self.phrase_bank, self.assistant_messages, self.audit_toggles, self.effective_msg
            )
        else:
            self.report, self.targets = AuditReport.clean(), []
        self.rewrite = _rewrite_due(
            self.report, length_guard_triggered=self.length_guard_triggered, can_rewrite=self.can_rewrite
        )
        self.request = _build_editor_request(
            self.report,
            self.targets,
            audit_enabled=self.audit_enabled,
            rewrite=self.rewrite,
            length_guard_triggered=self.length_guard_triggered,
            length_guard_instruction=self.length_guard_instruction,
            reasoning_on=self.reasoning_on,
        )

    async def initial_audit(self) -> None:
        """Audit the Writer's draft and record the report in the debug log."""
        if not self.audit_enabled:
            logger.info("Editor: audit disabled, skipping scanners")
            await self.audit()
            return
        logger.info(
            "Editor: audit on draft (%d chars), %d previous messages, %d phrase groups",
            len(self.draft),
            len(self.assistant_messages),
            len(self.phrase_bank),
        )
        await self.audit()
        _log_initial_audit(self.report, self.targets)
        self.debug_parts.append(f"Initial audit ({self.report.total_issues} issues):\n{self.request.report_text}")

    def skip_reason(self) -> str:
        """Why the loop has nothing to ask the model, or ``""`` when it does."""
        if self.report.is_clean and not self.length_guard_triggered:
            return "audit clean and no length guard"
        if not self.base.tools:
            return "no editor tools applicable"
        if not self.targets and not self.rewrite:
            return f"{self.report.total_issues} issue(s), none addressable"
        return ""

    def start(self, writer_user_msg: str | list[ContentPart]) -> None:
        """Open the replay buffer: the Writer's request, its draft, and the first edit request."""
        self.trailing = [
            {"role": "user", "content": writer_user_msg},
            {"role": "assistant", "content": self.draft},
            {"role": "user", "content": self.request.prompt},
        ]
        self.rules_shown = set(self.request.ruled) if self.request.ruled is not None else None
        self.prev_issues = self.report.total_issues

    async def run(self) -> AsyncIterator[Mapping[str, Any]]:
        """Iterate until an iteration stops the loop or MAX_EDITOR_ITERATIONS run out.

        A failed iteration stops editing but keeps what the finished iterations produced.
        """
        for n in range(1, MAX_EDITOR_ITERATIONS + 1):
            if self.client.is_aborted:
                logger.info("Editor: abort signal detected at iteration %d, stopping", n)
                return
            if logger.isEnabledFor(logging.DEBUG):
                messages = [*self.base.prefix, *self.trailing]
                logger.debug(
                    "Editor iteration %d/%d, %d issues remaining, sending %d messages to LLM:\n%s",
                    n,
                    MAX_EDITOR_ITERATIONS,
                    self.report.total_issues,
                    len(messages),
                    json.dumps(messages, default=str, indent=2),
                )
            try:
                resp: CompletionMessage = {}
                async for event in self.base.complete_into(
                    self.client,
                    resp,
                    label="editor",
                    trailing=self.trailing,
                    tool_choice=_pick_tool_choice(self.rewrite, self.audit_enabled),
                    kv_tracker=self.kv_tracker,
                    **self.params,
                ):
                    yield event
                settle = self._apply(resp, n)
                if settle is None:
                    return
                yield {"type": "draft_update", "draft": self.draft}
                if not await settle():
                    return
            except Exception as e:
                logger.error("Editor iteration %d failed: %s", n, e, exc_info=True)
                self.debug_parts.append(f"Iteration {n} error: {e}")
                during = "output_auditor" if self.audit_enabled else "length_guard"
                yield {"type": "failure", "during": during, "label": "", "error": e}
                return
        logger.warning(
            "Editor: hit max iterations (%d) with %d issues remaining", MAX_EDITOR_ITERATIONS, self.report.total_issues
        )

    def _apply(self, resp: CompletionMessage, n: int) -> Callable[[], Awaitable[bool]] | None:
        """Apply the edit *resp* calls for and return the re-audit that settles it; None when the loop stops here instead."""
        # A stop cuts the call short: a half-streamed rewrite or patch list is
        # not an edit, so the draft stays what the finished iterations made it.
        if self.client.is_aborted:
            logger.info("Editor iteration %d: stopped mid-call, discarding its output", n)
            return None
        self.debug_parts.append(f"Iteration {n} response:\n{json.dumps(resp, default=str)}")
        finish_reason = resp.get("finish_reason") or resp.get("stop_reason")
        if finish_reason:
            logger.info("Editor iteration %d: finish_reason=%s", n, finish_reason)

        parsed = parse_tool_calls(resp)
        if not parsed:
            outcome = "empty" if not resp else f"finish_reason={finish_reason}"
            logger.info("Editor iteration %d: no tool call (resp=%s), stopping", n, outcome)
            return None
        self.calls.extend(parsed)
        if rewrite_call := next((tc for tc in parsed if tc["name"] == "editor_rewrite"), None):
            return self._apply_rewrite(rewrite_call, resp, n)
        if patch_call := next((tc for tc in parsed if tc["name"] == "editor_apply_patch"), None):
            return self._apply_patches(patch_call, resp, n)
        logger.info("Editor iteration %d: unrecognised tool call, stopping", n)
        return None

    def _apply_rewrite(self, call: ParsedToolCall, resp: CompletionMessage, n: int) -> Callable[[], Awaitable[bool]] | None:
        # An explicit ``"rewritten_text": null`` is the model declining the forced call, and reads the same as the empty string
        # -- the default only covers an absent key, so coerce before .strip() rather than after.
        raw_rewrite = call.get("arguments", {}).get("rewritten_text")
        rewritten = raw_rewrite.strip() if isinstance(raw_rewrite, str) else ""
        if not rewritten:
            logger.info("Editor iteration %d: empty rewrite, stopping", n)
            return None
        pre_len = len(self.draft)
        self.draft = rewritten
        self.length_guard_triggered = False
        logger.info("Editor iteration %d: rewrite applied, draft %d→%d chars", n, pre_len, len(rewritten))
        self.debug_parts.append(f"Iteration {n}: rewrite applied ({pre_len}→{len(rewritten)} chars)")
        return partial(self._after_rewrite, resp)

    def _apply_patches(self, call: ParsedToolCall, resp: CompletionMessage, n: int) -> Callable[[], Awaitable[bool]] | None:
        patches = call.get("arguments", {}).get("patches", [])
        if not patches:
            logger.info("Editor iteration %d: empty patches, stopping", n)
            return None
        pre_len = len(self.draft)
        # `targets` are the ids that numbered the report this call answered;
        # the re-audit in _after_patches replaces them for the next iteration.
        self.draft, errors = apply_id_patches(self.draft, self.targets, patches)
        logger.info("Editor iteration %d: applied %d patches, draft %d→%d chars", n, len(patches), pre_len, len(self.draft))
        for e in errors:
            logger.warning("Editor iteration %d patch error: %s", n, e)
        return partial(self._after_patches, resp, errors, n)

    async def _after_rewrite(self, resp: CompletionMessage) -> bool:
        await self.audit()
        if self.audit_enabled:
            self.debug_parts.append(f"Post-rewrite audit ({self.report.total_issues} issues):\n{self.request.report_text}")
        if self.report.is_clean or self._none_addressable():
            return False
        self._queue(resp, [])
        return True

    async def _after_patches(self, resp: CompletionMessage, errors: list[PatchError], n: int) -> bool:
        await self.audit()
        logger.info("Editor iteration %d: post-audit — %d issues", n, self.report.total_issues)
        self.debug_parts.append(f"Post-iteration {n} audit ({self.report.total_issues} issues):\n{self.request.report_text}")
        explain_rejection = self._explain_rejection(errors, n)
        if self.report.is_clean and not explain_rejection:
            if not self.length_guard_triggered:
                return False
            # Audit clean but length guard still pending: next iteration's tool_choice forces editor_rewrite
            # (length_guard_triggered is still True). The schema blob is left untouched so the KV cache survives the hand-off.
            logger.info("Editor: audit clean, length guard still pending — queuing rewrite")
        if self._none_addressable():
            return False
        if self.report.total_issues >= self.prev_issues and not explain_rejection:
            logger.info("Editor: no progress (%d → %d issues), stopping", self.prev_issues, self.report.total_issues)
            return False
        self._queue(resp, errors)
        return True

    def _none_addressable(self) -> bool:
        if self.targets or self.rewrite:
            return False
        logger.info("Editor: %d issue(s) left, none addressable, stopping", self.report.total_issues)
        return True

    def _explain_rejection(self, errors: list[PatchError], n: int) -> bool:
        """Whether to spend one extra structured iteration delivering a protected-sequence rejection via tool-result errors.

        Only while a target remains; otherwise the unchanged issue count would stop before feedback reaches the model. Flat
        recaps have no tool-result slot and keep the original span.
        """
        rejected = [e for e in errors if e.kind == PatchErrorKind.PROTECTED_SEQUENCE]
        if not (rejected and self.reasoning_on and not self.guard_retry_spent and self.targets):
            return False
        self.guard_retry_spent = True
        logger.info(
            "Editor iteration %d: %d patch(es) rejected by the protected-sequence guard, continuing once to tell the model why",
            n,
            len(rejected),
        )
        self.debug_parts.append("Protected-sequence rejection replayed to the model:\n" + "\n".join(rejected))
        return True

    def _queue(self, resp: CompletionMessage, errors: list[PatchError]) -> None:
        """Hand the model the edited draft's re-audit for the next call.

        The structured replay shows the model its exact call, in the form it was trained on, with the re-audit as every tool
        call's result (see :func:`_tool_result_text`). It is the one replay that shows the model its old ids next to a fresh
        report, so the result states the renumbering rather than leaving it to be inferred. The flat recap swaps the draft and
        the request in place so the message list stays flat.
        """
        self.prev_issues = self.report.total_issues
        if not self.reasoning_on:
            self.trailing[-2] = {"role": "assistant", "content": self.draft}
            self.trailing[-1] = {"role": "user", "content": self.request.prompt}
            return
        tool_calls = resp.get("tool_calls", [])
        recap: AssistantToolMessage = {
            "role": "assistant",
            "content": resp.get("content") or "",
            "tool_calls": tool_calls,
            **replay_reasoning(resp),
        }
        self.trailing.append(recap)
        if not tool_calls:
            return
        rules, self.rules_shown = _owed_patch_rules(self.request.ruled, self.rules_shown)
        result = _tool_result_text(errors, self.request.report_text, renumbered=bool(self.targets), rules=rules)
        for tc in tool_calls:
            self.trailing.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})


def _log_initial_audit(report: AuditReport, targets: Sequence[Target]) -> None:
    logger.info(
        "Editor: initial audit — %d issues (cliches=%d, openers=%d, templates=%d, not_but=%d, phrases=%d, echoes=%d, "
        "structural=%d, negated=%d) → %d target(s)",
        report.total_issues,
        report.cliche_result.flagged_count,
        len(report.monotony_result.flagged_openers),
        len(report.template_result.flagged_templates),
        len(report.not_but_result),
        len(report.phrase_result.flagged_phrases) if report.phrase_result else 0,
        len(report.echo_result.flagged_echoes) if report.echo_result else 0,
        int(_structural_rewrite_needed(report)),
        len(report.negation_findings),
        len(targets),
    )


def _structural_rewrite_needed(report: AuditReport) -> bool:
    return report.structural_repetition_result is not None and report.structural_repetition_result.is_repetitive


def _rewrite_due(report: AuditReport, *, length_guard_triggered: bool, can_rewrite: bool) -> bool:
    """Whether this iteration forces ``editor_rewrite`` instead of patching.

    Only findings with no span to patch call for a whole-draft rewrite: an over-long draft, or structural repetition. Either
    needs *can_rewrite*, the shared tools blob carrying ``editor_rewrite``: forcing a tool the request does not carry gets prose
    back, never a call. A finding that resolved to no target is not one of them; with nothing else addressable the loop stops.
    """
    return can_rewrite and (length_guard_triggered or _structural_rewrite_needed(report))


def _pick_tool_choice(rewrite: bool, audit_enabled: bool):
    """Return the ``tool_choice`` value for the editor LLM call."""
    if rewrite:
        return require_tool("editor_rewrite")["choice"]
    if audit_enabled:
        return require_tool("editor_apply_patch")["choice"]
    return "auto"


def _build_editor_request(
    report: AuditReport,
    targets: Sequence[Target],
    *,
    audit_enabled: bool,
    rewrite: bool,
    length_guard_triggered: bool,
    length_guard_instruction: str,
    reasoning_on: bool,
) -> _Request:
    """The prompt and report for one editor iteration, rendered in lockstep.

    Kept as one call because the prompt's patch/rewrite branch and the report's rendering must agree. The report is numbered
    when the call will patch and sectioned when it will rewrite: the rewrite tool takes whole text and has no ids to address,
    and a flat numbered list cannot carry the structural-repetition finding at all. A numbered report beside rewrite
    instructions offers ids no tool can take, and a sectioned report beside patch instructions offers no ids at all. *rewrite*
    is :func:`_rewrite_due` for this report.
    """
    report_text = format_report(report) if rewrite else format_numbered_report(targets)
    has_issues = audit_enabled and not report.is_clean
    structural = rewrite and _structural_rewrite_needed(report)
    categories = frozenset(category for target in targets for category in target.categories)
    prompt = build_editor_prompt(
        has_issues,
        report_text,
        length_guard_triggered,
        length_guard_instruction,
        structural_rewrite=structural,
        reasoning_on=reasoning_on,
        patchable=bool(targets),
        patch_categories=categories,
    )
    patching = editor_patches(has_issues, length_guard_triggered, structural, bool(targets))
    return _Request(prompt, report_text, categories if patching else None)


def _owed_patch_rules(ruled: frozenset[str] | None, shown: set[str] | None) -> tuple[str, set[str] | None]:
    """``(rules, shown)``: what a replayed tool result must add before a patch request.

    The structured replay sends later reports as tool results, not fresh prompts, so the patching rules a new report needs --
    all of them after a rewrite request, else those of newly flagged kinds -- ride the result.
    """
    if ruled is None:
        return "", shown
    return patch_instructions(ruled, shown=shown), {*(shown or ()), *ruled}


def _tool_result_text(errors: Sequence[str], report_text: str, *, renumbered: bool, rules: str = "") -> str:
    """The tool-result content fed back on the structured-replay path.

    Apply errors first (they name the ids the model just used), then the renumbering notice when the report below carries fresh
    ids, then any patching rules the report needs that the conversation lacks, then the report.
    """
    parts = []
    if errors:
        parts.append("\n".join(errors))
    if renumbered:
        parts.append(EDITOR_RENUMBER_NOTICE)
    if rules:
        parts.append(rules)
    parts.append(report_text)
    return "\n\n".join(parts)
