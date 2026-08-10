"""
passes/world_change.py -- Dynamic Worlds proposal step.

Asks the model, via a forced ``propose_world_changes`` call, whether the turn
established anything durably true of the shared World. It runs last: after the
editor and after the draft-rewriting post-pipeline hooks, so the prose it judges
is the prose that will be persisted, not the writer's first draft.

The wire schema is the fixed ``propose_world_changes`` tool held in the shared
per-turn blob, so the call reuses the cached base and only forces the tool
choice; the catalog of existing entries rides this step's OOC trailing. The
exchange is replayed as the two messages before the request -- the writer's user
message and the final reply -- which both grounds the judgement in the persisted
text and extends the warm writer/editor prefix.

The step proposes; it never writes. Its output is validated in
``features/lorebook/proposals.py`` and staged as a *pending* changeset at the
same persistence boundary as the assistant reply. Errors and aborts are
swallowed into an empty proposal: this runs immediately before the turn's
``_result``, where a propagating exception would skip persisting the finished
reply, and a bookkeeping step must never cost the user their message.
"""

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

    ``operations`` are validated and ready to stage; an empty list is the normal
    "nothing durable happened" outcome and stages nothing. ``calls`` is the
    parsed tool call, appended to the turn's tool calls so the proposal stays
    visible in the inspector audit -- while the durable changeset lives in its
    own table, independent of reclaimable conversation logs.
    """

    summary: str = ""
    operations: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.operations


async def world_change_step(
    client: LLMClient,
    base: CachedBase,
    *,
    settings: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    reply_text: str,
    writer_user_msg: str | list[ContentPart] | None = None,
    original_user_message: str = "",
    exchange_text: str = "",
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[dict]:
    """Yield reasoning chunks during the call, then a single done dict.

    One forced ``propose_world_changes`` call. *entries* is the target World's
    full row set, read immediately before this runs so the catalog and the
    validation both reflect the latest content revision.

    Yields:
        ``{"type": "reasoning", "delta": str}``
        ``{"type": "done", "result": WorldChangeResult}``
    """
    catalog = build_world_change_catalog(entries, exchange_text=exchange_text)
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
        yield {"type": "done", "result": WorldChangeResult()}
        return

    logger.info("World-change step output:\n%s", json.dumps(resp, default=str))
    calls = parse_tool_calls(resp)
    checked = validate_proposal(parse_proposal_call(calls), entries)
    if checked.rejected:
        logger.info("World-change proposal dropped %d operation(s): %s", len(checked.rejected), checked.rejected)

    yield {
        "type": "done",
        "result": WorldChangeResult(summary=checked.summary, operations=checked.operations, calls=calls),
    }
