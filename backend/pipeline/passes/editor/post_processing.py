"""Run fragment-defined exact edits over the Writer draft."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ....analysis.detectors.subject_fixation import confirm, nominate, presence_streaks
from ....analysis.healing import heal_deletion
from ....analysis.text.markup import narration_mask
from ....core import ChatMessage, ContentPart, extract_hyperparams
from ....core.llm_types import CompletionMessage, ParsedToolCall
from ....core.settings import Settings
from ....inference import CachedBase, DecisionCancelled, LLMClient, parse_tool_calls, reasoning_cfg
from ....prompting.tool_schemas import EDITOR_FIND_REPLACE_CHOICE
from ...subject_tags import TaggedReply, tag_text
from ..judge import JudgeConfig
from .gate import GATE_BUDGET_SECONDS, gate_question, judge_gate
from .prompts import build_post_processing_prompt, build_subject_fixation_prompt
from .subject_repeats import repeat_scores

logger = logging.getLogger(__name__)


def post_processing_active(post_processing_fragments: Sequence[Mapping[str, Any]], *, agent_on: bool) -> bool:
    """Return whether fragment-defined Editor work should run this turn."""
    return agent_on and bool(post_processing_fragments)


def apply_find_replace_patches(draft: str, patches: object, *, label: str = "", narration_only: bool = False) -> str:
    """Apply valid exact patches sequentially, skipping every unsafe entry.

    A patch is safe only when it has string ``find`` and ``replace`` values, the find is non-empty and differs from the
    replacement, and the evolving draft contains exactly one case-sensitive match; with *narration_only*, edits must preserve
    the original draft's dialogue, including when a deletion heals its seam. Invalid entries do not prevent later valid patches
    from being considered; each one skipped is logged with its reason, under the fragment *label*.
    """
    if not isinstance(patches, list):
        if patches is not None:
            logger.warning("Post-processing %r: patches is not a list, nothing applied: %r", label, patches)
        return draft

    current = draft
    # Keep the original speech protected even if edits remove the markup that identified bare dialogue.
    mask = narration_mask(draft) if narration_only else None
    for index, patch in enumerate(patches):
        reason = ""
        if not isinstance(patch, Mapping):
            reason = "not a find/replace object"
        else:
            find = patch.get("find")
            replace = patch.get("replace")
            if not isinstance(find, str) or not isinstance(replace, str):
                reason = "find and replace must both be strings"
            elif not find:
                reason = "empty find"
            elif find == replace:
                reason = "replace repeats the find"
            else:
                first = current.find(find)
                if first < 0:
                    reason = "find not found in the draft"
                elif current.find(find, first + 1) >= 0:
                    reason = "find matches more than one place"
                elif mask is not None and not all(mask[first : first + len(find)]):
                    reason = "find reaches into dialogue"
                else:
                    start, end = first, first + len(find)
                    if not replace.strip():
                        start, end, replace = heal_deletion(current, start, end)
                    # Healing may trim boundary whitespace, but cannot change protected speech.
                    if mask is not None and any(not mask[i] and not current[i].isspace() for i in range(start, end)):
                        reason = "healed deletion reaches into dialogue"
                    else:
                        current = current[:start] + replace + current[end:]
                        if mask is not None:
                            mask[start:end] = [True] * len(replace)
        if reason:
            logger.warning("Post-processing %r: patch %d skipped (%s): %r", label, index, reason, patch)
    return current


def _apply_find_replace_calls(
    draft: str, calls: Sequence[ParsedToolCall], *, label: str = "", narration_only: bool = False
) -> str:
    """Apply every ``editor_find_replace`` call's patches to *draft* in order."""
    patches = []
    for call in calls:
        if call.get("name") == "editor_find_replace":
            entries = call.get("arguments", {}).get("patches")
            if isinstance(entries, list):
                patches.extend(entries)
            elif entries is not None:
                logger.warning("Post-processing %r: patches is not a list, nothing applied: %r", label, entries)
    return apply_find_replace_patches(draft, patches, label=label, narration_only=narration_only)


async def _find_replace_call(
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
    """One forced ``editor_find_replace`` call over *draft* that extends the Writer's prefix; the reply lands in *resp*."""
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
        tool_choice=EDITOR_FIND_REPLACE_CHOICE,
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
            async for event in _find_replace_call(
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
        current = _apply_find_replace_calls(current, parsed, label=fragment.get("label") or fragment.get("id", ""))
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
    history: Sequence[TaggedReply],
    *,
    writer_user_msg: str | list[ContentPart],
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[Mapping[str, Any]]:
    """Tag *draft*; flag subjects every recent reply in *history* (newest first) also had, and ask the pair comparer whether
    other descriptions repeat a detail from those replies. One forced exact-edit call reduces the flagged focus in narration
    while preserving important actions and new information.

    Nothing to cut changes nothing. A stop keeps the draft."""
    tags = await tag_text(draft)
    history_tags = [reply.probs for reply in history]
    streaks = presence_streaks(tags, history_tags)
    present = {streak.category for streak in streaks}
    nominees = [category for category in nominate(tags, history_tags) if category not in present]
    if nominees:
        scores = await repeat_scores(draft, [reply.text for reply in history], nominees)
        if client.is_aborted:
            return
        streaks += [streak for category in nominees if (streak := confirm(category, scores[category]))]
    if not streaks:
        return
    yield {"type": "step", "step": "subject_fixation"}
    resp: CompletionMessage = {}
    async for event in _find_replace_call(
        client,
        base,
        resp,
        draft,
        build_subject_fixation_prompt(streaks, reasoning_on=reasoning_on),
        settings,
        writer_user_msg=writer_user_msg,
        kv_tracker=kv_tracker,
        reasoning_on=reasoning_on,
        reasoning_prefill=reasoning_prefill,
    ):
        yield event
    calls = [] if client.is_aborted else parse_tool_calls(resp)
    edited = _apply_find_replace_calls(draft, calls, label="subject_fixation", narration_only=True)
    if edited != draft:
        yield {"type": "draft_update", "draft": edited}
    logger.info("Subject fixation on %s: changed=%s", [streak.category for streak in streaks], edited != draft)
    yield {"type": "done", "result": PostProcessingResult(draft=edited, tool_calls=calls)}
