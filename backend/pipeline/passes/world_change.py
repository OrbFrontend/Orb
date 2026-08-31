"""Propose Dynamic Worlds changes after the final draft is ready."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...core import ChatMessage, ContentPart, extract_hyperparams
from ...features.lorebook import (
    build_world_change_catalog,
    parse_proposal_call,
    validate_proposal,
)
from ...inference import (
    PROPOSE_WORLD_CHANGES_CHOICE,
    CachedBase,
    LLMClient,
    build_world_change_prompt,
    parse_tool_calls,
    reasoning_cfg,
)
from ...inference.tool_registry import PROPOSE_WORLD_CHANGES_TOOL

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorldChangeResult:
    """Typed result of the proposal step, yielded as the ``done`` payload.

    ``operations`` are validated and ready to stage, each stamped with the World
    it belongs to when the step was given more than one; an empty list is the
    normal "nothing durable happened" outcome and stages nothing. ``calls`` is
    the parsed tool call, appended to the turn's tool calls so the proposal stays
    visible in the inspector audit -- while the durable changeset lives in its
    own table, independent of reclaimable conversation logs.
    """

    summary: str = ""
    operations: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    failed: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.operations


async def world_change_step(
    client: LLMClient,
    base: CachedBase,
    *,
    settings: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    worlds: Sequence[Mapping[str, Any]] = (),
    reply_text: str,
    writer_user_msg: str | list[ContentPart] | None = None,
    original_user_message: str = "",
    exchange_text: str = "",
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[dict]:
    """Yield reasoning chunks during the call, then a single done dict.

    One forced call however many Worlds are in play — a turn's opted-in Worlds
    share one catalog and one judgement. *entries* is the pooled row set of every
    World in *worlds*; each returned operation comes back stamped with the one it
    belongs to (see ``features/lorebook/proposals.split_by_world``).

    Yields:
        ``{"type": "reasoning", "delta": str}``
        ``{"type": "done", "result": WorldChangeResult}``
    """
    catalog = build_world_change_catalog(entries, worlds=worlds, exchange_text=exchange_text)
    request = build_world_change_prompt(
        catalog,
        original_user_message=original_user_message,
        reasoning_on=reasoning_on,
        tool_schema=PROPOSE_WORLD_CHANGES_TOOL,
    )
    trailing: list[ChatMessage] = [
        {"role": "user", "content": writer_user_msg or ""},
        {"role": "assistant", "content": reply_text},
        {"role": "user", "content": request},
    ]
    hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.3, "max_tokens": 2048})

    resp: dict = {}
    try:
        async for event in base.complete_into(
            client,
            resp,
            label="propose_world_changes",
            trailing=trailing,
            tool_choice=PROPOSE_WORLD_CHANGES_CHOICE,
            kv_tracker=kv_tracker,
            **hyperparams,
            **reasoning_cfg(reasoning_on, reasoning_prefill),
        ):
            yield event
    except Exception:
        logger.exception("World-change proposal call failed; proposing nothing")
        yield {"type": "done", "result": WorldChangeResult(failed=True)}
        return

    logger.info("World-change step output:\n%s", json.dumps(resp, default=str))
    calls = parse_tool_calls(resp)
    proposal_call = parse_proposal_call(calls)
    checked = validate_proposal(proposal_call, entries, worlds=worlds)
    if checked.rejected:
        logger.info("World-change proposal dropped %d operation(s): %s", len(checked.rejected), checked.rejected)

    yield {
        "type": "done",
        "result": WorldChangeResult(
            summary=checked.summary,
            operations=checked.operations,
            calls=calls,
            failed=proposal_call is None or (bool(checked.rejected) and checked.is_empty),
        ),
    }
