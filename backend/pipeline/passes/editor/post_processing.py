"""Run fragment-defined exact edits over the Writer draft."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ....core import ChatMessage, ContentPart, extract_hyperparams
from ....inference import CachedBase, LLMClient, parse_tool_calls, reasoning_cfg
from ....prompting.tool_schemas import EDITOR_SEARCH_REPLACE_CHOICE
from .prompts import build_post_processing_prompt

logger = logging.getLogger(__name__)


def post_processing_active(
    post_processing_fragments: Sequence[Mapping[str, Any]],
    *,
    agent_on: bool,
) -> bool:
    """Return whether fragment-defined Editor work should run this turn."""
    return agent_on and bool(post_processing_fragments)


def apply_search_replace_patches(draft: str, patches: object) -> str:
    """Apply valid exact patches sequentially, skipping every unsafe entry.

    A patch is safe only when it has string ``search`` and ``replace`` values,
    the search is non-empty and differs from the replacement, and the evolving
    draft contains exactly one case-sensitive match. Invalid entries do not
    prevent later valid patches from being considered.
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
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[dict]:
    """Run one forced exact-edit call per fragment in ``sort_order``."""
    current = draft
    all_calls: list[dict] = []
    fragments = sorted(post_processing_fragments, key=lambda item: item.get("sort_order", 0))

    for fragment in fragments:
        if client.is_aborted:
            break

        request = build_post_processing_prompt(fragment, reasoning_on=reasoning_on)
        trailing: list[ChatMessage] = [
            {"role": "user", "content": writer_user_msg},
            {"role": "assistant", "content": current},
            {"role": "user", "content": request},
        ]
        hyperparams = extract_hyperparams(settings, lane="agent", defaults={"temperature": 0.25})
        resp: dict = {}
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
