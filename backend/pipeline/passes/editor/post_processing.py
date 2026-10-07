"""Run fragment-defined exact edits over the Writer draft."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ....analysis.detectors.subject_fixation import SubjectProbs, detect_subject_fixation
from ....analysis.text.markup import narration_mask
from ....core import ChatMessage, ContentPart, extract_hyperparams
from ....core.llm_types import CompletionMessage, ParsedToolCall
from ....core.settings import Settings
from ....inference import CachedBase, DecisionCancelled, LLMClient, parse_tool_calls, reasoning_cfg
from ....prompting.tool_schemas import EDITOR_SEARCH_REPLACE_CHOICE
from ...subject_tags import tag_text
from ..judge import JudgeConfig
from .gate import GATE_BUDGET_SECONDS, gate_question, judge_gate
from .prompts import build_post_processing_prompt, build_subject_fixation_prompt

logger = logging.getLogger(__name__)


def post_processing_active(post_processing_fragments: Sequence[Mapping[str, Any]], *, agent_on: bool) -> bool:
    """Return whether fragment-defined Editor work should run this turn."""
    return agent_on and bool(post_processing_fragments)


def apply_search_replace_patches(draft: str, patches: object, *, label: str = "", narration_only: bool = False) -> str:
    """Apply valid exact patches sequentially, skipping every unsafe entry.

    A patch is safe only when it has string ``search`` and ``replace`` values, the search is non-empty and differs from the
    replacement, and the evolving draft contains exactly one case-sensitive match; with *narration_only*, that match must also
    lie outside quoted speech. Invalid entries do not prevent later valid patches from being considered; each one skipped is
    logged with its reason, under the fragment *label*.
    """
    if not isinstance(patches, list):
        if patches is not None:
            logger.warning("Post-processing %r: patches is not a list, nothing applied: %r", label, patches)
        return draft

    current = draft
    for index, patch in enumerate(patches):
        reason = ""
        if not isinstance(patch, Mapping):
            reason = "not a search/replace object"
        else:
            search = patch.get("search")
            replace = patch.get("replace")
            if not isinstance(search, str) or not isinstance(replace, str):
                reason = "search and replace must both be strings"
            elif not search:
                reason = "empty search"
            elif search == replace:
                reason = "replace repeats the search"
            else:
                first = current.find(search)
                if first < 0:
                    reason = "search not found in the draft"
                elif current.find(search, first + 1) >= 0:
                    reason = "search matches more than one place"
                elif narration_only and not all(narration_mask(current)[first : first + len(search)]):
                    reason = "search reaches into dialogue"
                else:
                    current = current[:first] + replace + current[first + len(search) :]
        if reason:
            logger.warning("Post-processing %r: patch %d skipped (%s): %r", label, index, reason, patch)
    return current


def _apply_search_replace_calls(
    draft: str, calls: Sequence[ParsedToolCall], *, label: str = "", narration_only: bool = False
) -> str:
    """Apply every ``editor_search_replace`` call's patches to *draft* in order."""
    for call in calls:
        if call.get("name") == "editor_search_replace":
            draft = apply_search_replace_patches(
                draft, call.get("arguments", {}).get("patches"), label=label, narration_only=narration_only
            )
    return draft


async def _search_replace_call(
    client: LLMClient,
    base: CachedBase,
    resp: CompletionMessage,
    draft: str,
    prompt: str,
    settings: Settings,
    *,
    writer_user_msg: str | list[ContentPart],
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[Mapping[str, Any]]:
    """One forced ``editor_search_replace`` call over *draft* that extends the Writer's prefix; the reply lands in *resp*."""
    trailing: list[ChatMessage] = [
        {"role": "user", "content": writer_user_msg},
        {"role": "assistant", "content": draft},
        {"role": "user", "content": prompt},
    ]
    async for event in base.complete_into(
        client,
        resp,
        label="editor",
        trailing=trailing,
        tool_choice=EDITOR_SEARCH_REPLACE_CHOICE,
        kv_tracker=kv_tracker,
        **extract_hyperparams(settings, lane="agent"),
        **reasoning_cfg(reasoning_on, reasoning_prefill),
    ):
        yield event


@dataclass(slots=True)
class PostProcessingResult:
    """The evolving draft and normalized calls produced by all fragments."""

    draft: str
    tool_calls: list[ParsedToolCall] = field(default_factory=list)


async def post_processing_step(
    client: LLMClient,
    base: CachedBase,
    draft: str,
    settings: Settings,
    post_processing_fragments: Sequence[Mapping[str, Any]],
    *,
    writer_user_msg: str | list[ContentPart],
    effective_msg: str,
    judge_config: JudgeConfig | None = None,
    recent_replies: Sequence[str] = (),
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[Mapping[str, Any]]:
    """Run one forced exact-edit call per fragment in ``sort_order``.

    A fragment with a gate question first asks the Judge about the draft as the earlier fragments left it, and is skipped on a
    no. The gate may also show the Judge some of *recent_replies* (newest first). Every gate in the step shares one
    ``GATE_BUDGET_SECONDS`` of Judge waiting.

    A fragment whose call fails is reported as a ``failure`` event and skipped; the draft keeps the earlier fragments' edits and
    the later ones still run.
    """
    current = draft
    all_calls: list[ParsedToolCall] = []
    fragments = sorted(post_processing_fragments, key=lambda item: item.get("sort_order", 0))
    judge_allowance = GATE_BUDGET_SECONDS

    for fragment in fragments:
        if client.is_aborted:
            break

        if gate_question(fragment):
            started = time.monotonic()
            try:
                record = await judge_gate(
                    judge_config,
                    fragment,
                    effective_msg=effective_msg,
                    draft=current,
                    timeout_seconds=judge_allowance,
                    recent_replies=recent_replies,
                    abort=client.abort_token,
                )
            except DecisionCancelled:
                break
            finally:
                judge_allowance = max(0.0, judge_allowance - (time.monotonic() - started))
            all_calls.append(record)
            if client.is_aborted:
                break
            if not record["arguments"]["fired"]:
                logger.info("Post-processing fragment %r skipped by its gate", fragment.get("id", ""))
                continue

        resp: CompletionMessage = {}
        try:
            async for event in _search_replace_call(
                client,
                base,
                resp,
                current,
                build_post_processing_prompt(fragment, reasoning_on=reasoning_on),
                settings,
                writer_user_msg=writer_user_msg,
                kv_tracker=kv_tracker,
                reasoning_on=reasoning_on,
                reasoning_prefill=reasoning_prefill,
            ):
                yield event
        except Exception as exc:
            logger.exception("Post-processing fragment %r failed; skipping it", fragment.get("id", ""))
            label = fragment.get("label") or fragment.get("id", "")
            yield {"type": "failure", "during": "post_processing", "label": label, "error": exc}
            continue
        # A stop cuts the call short; its edits may be half-written, so the
        # draft keeps the earlier fragments' finished edits only.
        if client.is_aborted:
            break

        parsed = parse_tool_calls(resp)
        all_calls.extend(parsed)
        before = current
        current = _apply_search_replace_calls(current, parsed, label=fragment.get("label") or fragment.get("id", ""))
        if current != before:
            yield {"type": "draft_update", "draft": current}

        logger.info(
            "Post-processing fragment %r completed (changed=%s): %s",
            fragment.get("id", ""),
            current != before,
            json.dumps(resp, default=str),
        )

    yield {"type": "done", "result": PostProcessingResult(draft=current, tool_calls=all_calls)}


async def subject_fixation_step(
    client: LLMClient,
    base: CachedBase,
    draft: str,
    settings: Settings,
    history_tags: Sequence[SubjectProbs],
    *,
    writer_user_msg: str | list[ContentPart],
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[Mapping[str, Any]]:
    """Tag *draft* and, when it extends a streak in *history_tags* (newest first), cut those subjects with one forced exact-edit
    call over the narration. No streak yields nothing. A stop mid-call keeps the draft."""
    streaks = detect_subject_fixation(await tag_text(draft), history_tags)
    if not streaks:
        return
    yield {"type": "step", "step": "subject_fixation"}
    resp: CompletionMessage = {}
    async for event in _search_replace_call(
        client,
        base,
        resp,
        draft,
        build_subject_fixation_prompt([streak.reason for streak in streaks], reasoning_on=reasoning_on),
        settings,
        writer_user_msg=writer_user_msg,
        kv_tracker=kv_tracker,
        reasoning_on=reasoning_on,
        reasoning_prefill=reasoning_prefill,
    ):
        yield event
    calls = [] if client.is_aborted else parse_tool_calls(resp)
    edited = _apply_search_replace_calls(draft, calls, label="subject_fixation", narration_only=True)
    if edited != draft:
        yield {"type": "draft_update", "draft": edited}
    remaining = None
    try:
        # The save stores this reading, so the saved reply is not tagged again.
        remaining = [streak.category for streak in detect_subject_fixation(await tag_text(edited), history_tags)]
    except Exception:
        logger.exception("Subject tagging of the edited draft failed")
    logger.info(
        "Subject fixation on %s: changed=%s, still streaking=%s",
        [streak.category for streak in streaks],
        edited != draft,
        remaining,
    )
    yield {"type": "done", "result": PostProcessingResult(draft=edited, tool_calls=calls)}
