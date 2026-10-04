"""Run fragment-defined exact edits over the Writer draft."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ....core import ChatMessage, ContentPart, extract_hyperparams
from ....inference import CachedBase, DecisionCancelled, LLMClient, parse_tool_calls, reasoning_cfg
from ....prompting.tool_schemas import EDITOR_SEARCH_REPLACE_CHOICE
from ..judge import JudgeConfig
from .gate import GATE_BUDGET_SECONDS, gate_question, judge_gate
from .prompts import build_post_processing_prompt

logger = logging.getLogger(__name__)


def post_processing_active(post_processing_fragments: Sequence[Mapping[str, Any]], *, agent_on: bool) -> bool:
    """Return whether fragment-defined Editor work should run this turn."""
    return agent_on and bool(post_processing_fragments)


def apply_search_replace_patches(draft: str, patches: object) -> str:
    """Apply valid exact patches sequentially, skipping every unsafe entry.

    A patch is safe only when it has string ``search`` and ``replace`` values, the search is non-empty and differs from the
    replacement, and the evolving draft contains exactly one case-sensitive match. Invalid entries do not prevent later valid
    patches from being considered.
    """
    if not isinstance(patches, list):
        return draft

    current = draft
    for patch in patches:
        if not isinstance(patch, Mapping):
            continue
        search = patch.get("search")
        replace = patch.get("replace")
        if not isinstance(search, str) or not isinstance(replace, str):
            continue
        if not search or search == replace:
            continue
        first = current.find(search)
        if first < 0 or current.find(search, first + 1) >= 0:
            continue
        current = current[:first] + replace + current[first + len(search) :]
    return current


@dataclass(slots=True)
class PostProcessingResult:
    """The evolving draft and normalized calls produced by all fragments."""

    draft: str
    tool_calls: list[dict] = field(default_factory=list)


async def post_processing_step(
    client: LLMClient,
    base: CachedBase,
    draft: str,
    settings: Mapping[str, Any],
    post_processing_fragments: Sequence[Mapping[str, Any]],
    *,
    writer_user_msg: str | list[ContentPart],
    effective_msg: str,
    judge_config: JudgeConfig | None = None,
    recent_replies: Sequence[str] = (),
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[dict]:
    """Run one forced exact-edit call per fragment in ``sort_order``.

    A fragment with a gate question first asks the Judge about the draft as the earlier fragments left it, and is skipped on a
    no. The gate may also show the Judge some of *recent_replies* (newest first). Every gate in the step shares one
    ``GATE_BUDGET_SECONDS`` of Judge waiting.

    A fragment whose call fails is reported as a ``failure`` event and skipped; the draft keeps the earlier fragments' edits and
    the later ones still run.
    """
    current = draft
    all_calls: list[dict] = []
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

        edit_prompt = build_post_processing_prompt(fragment, reasoning_on=reasoning_on)
        trailing: list[ChatMessage] = [
            {"role": "user", "content": writer_user_msg},
            {"role": "assistant", "content": current},
            {"role": "user", "content": edit_prompt},
        ]
        hyperparams = extract_hyperparams(settings, lane="agent", defaults={"temperature": 0.25})
        resp: dict = {}
        try:
            async for event in base.complete_into(
                client,
                resp,
                label="editor",
                trailing=trailing,
                tool_choice=EDITOR_SEARCH_REPLACE_CHOICE,
                kv_tracker=kv_tracker,
                **hyperparams,
                **reasoning_cfg(reasoning_on, reasoning_prefill),
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
        for call in parsed:
            if call.get("name") == "editor_search_replace":
                current = apply_search_replace_patches(current, call.get("arguments", {}).get("patches"))
        if current != before:
            yield {"type": "draft_update", "draft": current}

        logger.info(
            "Post-processing fragment %r completed (changed=%s): %s",
            fragment.get("id", ""),
            current != before,
            json.dumps(resp, default=str),
        )

    yield {"type": "done", "result": PostProcessingResult(draft=current, tool_calls=all_calls)}
