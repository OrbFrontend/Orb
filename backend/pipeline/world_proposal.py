"""
world_proposal.py — the Dynamic Worlds stage of a turn.

Sits between the orchestrator and ``passes/world_change.py``: decides whether
the stage runs at all, re-reads the target Worlds, drives the forced tool call,
and parks the validated results on :class:`TurnState` for persistence to stage.

A turn may have several opted-in Worlds in play. They share **one** call — the
judgement is about the exchange, not about a book, and asking N times would cost
N generations to answer the same question — and the validated operations are
split back out into one pending changeset per World afterwards, because that is
the unit the revision check and the review queue both work in.

Nothing here writes to the World. The stage produces *pending* proposals;
applying one is always a separate, reviewed action (``features/lorebook``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from .. import database as db
from ..database.models import WorldRow
from ..features.lorebook import (
    dynamic_enabled,
    split_by_world,
    supersede_proposal,
)
from ..inference import (
    CachedBase,
    agent_lane_from_settings,
    client_from_settings,
    enabled_schemas,
)
from ..workflows.toolkit import build_offturn_prefix
from .context import conversation_macro_seed, persona_macros, resolve_card_and_persona
from .passes.world_change import world_change_step
from .state import TurnState, WorldProposalTurn, _PipelineConfig

logger = logging.getLogger(__name__)


async def _load_targets(
    world_ids: Sequence[str],
) -> tuple[list[WorldRow], list[Mapping[str, Any]]]:
    """Re-read every target World and pool their rows, in the given order.

    The turn-level tool blob was fixed when generation began, but the user's
    opt-in is authoritative at the moment this optional call would run. A
    cross-tab disable (of the World, or of its Dynamic Worlds switch) during a
    long writer/editor pass must drop that World even though the schema remains
    harmlessly present in the blob.
    """
    worlds: list[WorldRow] = []
    entries: list[Mapping[str, Any]] = []
    for world_id in world_ids:
        world = await db.get_world(world_id)
        if world is None or not world.get("enabled") or not dynamic_enabled(world):
            continue
        worlds.append(world)
        entries.extend(await db.get_lorebook_entries(world_id))
    return worlds, entries


async def world_proposal_stage(
    cfg: _PipelineConfig,
    state: TurnState,
    *,
    settings: Mapping[str, Any],
    turn: WorldProposalTurn,
    kv_tracker=None,
) -> AsyncIterator[dict]:
    """Run the proposal step and record its results on *state*.

    Yields the step's reasoning under the ``editor`` pass label (it shares the
    agent lane and the post-turn placement with the editor's own sub-steps), and
    appends the parsed tool call to ``state.calls`` so the inspector shows it
    alongside the rest of the turn.

    Every failure path is contained: no readable target World, a model that calls
    nothing, a call that validates to zero operations, and an outright transport
    error all leave ``state.world_proposals`` empty. This runs immediately before
    the turn's ``_result``, so it must never be able to cost the user their reply.
    """
    try:
        worlds, entries = await _load_targets(turn.world_ids)
    except Exception:
        logger.exception(
            "World-change stage could not load worlds %s; proposing nothing",
            list(turn.world_ids),
        )
        return
    if not worlds:
        return

    writer_msg = state.writer_content or state.user_message
    # The steered paths hand the writer an OOC instruction; the step must judge
    # what the user actually said instead (see build_world_change_prompt).
    # Compared against ``state.user_message`` -- always the writer's *text* input
    # -- rather than ``writer_content``, which is a multimodal parts list on a
    # turn carrying attachments and would read as unsteered.
    steered = state.user_message.strip() != turn.user_message.strip()

    result = None
    async for ev in world_change_step(
        cfg.agent_lane.client,
        cfg.agent_lane.base,
        settings=settings,
        entries=entries,
        worlds=worlds,
        reply_text=state.resp_text,
        writer_user_msg=writer_msg,
        original_user_message=turn.user_message if steered else "",
        exchange_text=f"{turn.user_message}\n{state.resp_text}",
        kv_tracker=kv_tracker,
        reasoning_on=cfg.editor_reasoning_on,
        reasoning_prefill=cfg.editor_reasoning_prefill,
    ):
        if ev["type"] == "reasoning":
            yield {
                "event": "reasoning",
                "data": {"pass": "editor", "delta": ev["delta"]},
            }
        elif ev["type"] == "done":
            result = ev["result"]

    if result is None:
        return
    if result.calls:
        state.calls.extend(result.calls)
    if result.is_empty:
        return

    # One changeset per World touched: each carries its own World's revision, so
    # a concurrent edit to one World cannot stale a proposal about another.
    by_world = {str(w["id"]): w for w in worlds}
    state.world_proposals = [
        {
            "world_id": world_id,
            "base_revision": int(by_world[world_id]["content_revision"]),
            "summary": result.summary,
            "operations": operations,
            "source_conversation_id": turn.conversation_id,
            "source_character_label": turn.character_label,
            "source_conversation_label": turn.conversation_label,
        }
        for world_id, operations in split_by_world(result.operations).items()
        if world_id in by_world
    ]


async def reevaluate_changeset(changeset: Mapping[str, Any]):
    """Re-run the proposal step for a stale changeset against the current World.

    The replacement for automatic rebasing. Two changes that touch different
    entries can still contradict each other in meaning, so a proposal that lost
    its revision race is not replayed — it is re-derived from its stored source
    messages against the World as it now stands, and comes back as a fresh
    pending changeset (linked via ``supersedes_changeset_id``).

    Scoped to the changeset's own World, even when the turn that produced it had
    others in play: the user is re-judging *this* proposal, and a re-evaluation
    that silently opened a second World's queue would be a surprise.

    Returns the new changeset, or ``None`` when the model validly proposed no
    operations this time. Either result atomically moves the original to
    terminal ``superseded``; a concurrent decision wins instead without leaking
    a replacement row. Missing source context and failed/malformed model calls
    raise while leaving the original open for retry or dismissal.
    """
    changeset_id = int(changeset["id"])

    async def retire() -> None:
        await supersede_proposal(changeset_id, None)

    asst_id = changeset.get("source_assistant_message_id")
    user_id = changeset.get("source_user_message_id")
    if asst_id is None:
        raise db.OverlayStateConflict("cannot re-evaluate: the source assistant message is gone")
    assistant_msg = await db.get_message_by_id(int(asst_id))
    user_msg = await db.get_message_by_id(int(user_id)) if user_id is not None else None
    if assistant_msg is None or user_msg is None:
        raise db.OverlayStateConflict("cannot re-evaluate: the source exchange is gone")

    world_id = changeset["world_id"]
    world = await db.get_world(world_id)
    if world is None:
        raise db.OverlayStateConflict(f"cannot re-evaluate: world {world_id} is gone")
    entries = await db.get_lorebook_entries(world_id)

    settings = await db.get_settings()
    conversation_id = assistant_msg["conversation_id"]
    conv = await db.get_conversation(conversation_id)
    if conv is None:
        raise db.OverlayStateConflict("cannot re-evaluate: the source conversation is gone")
    # History up to (but not including) the source exchange: the exchange itself
    # is the step's trailing, exactly as it is during a live turn.
    parent_id = user_msg.get("parent_id")
    history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []
    prefix = await build_offturn_prefix(conversation_id, history, settings, lane="agent")
    if not prefix:
        raise db.OverlayStateConflict("cannot re-evaluate: the Agent context is unavailable")

    _, persona = await resolve_card_and_persona(conv, settings)
    macros, _ = persona_macros(settings, conv["character_name"], persona, seed=conversation_macro_seed(conv))
    client = client_from_settings(settings)
    agent_client, model = agent_lane_from_settings(settings, writer_client=client)
    base = CachedBase(
        prefix=tuple(prefix),
        # The same enabled blob a live turn on this conversation would send, so
        # the re-evaluation extends that conversation's warm cached bottom
        # instead of paying for a fresh prefix.
        tools=tuple(enabled_schemas({**(settings.get("enabled_tools") or {}), "propose_world_changes": True})),
        model=model,
        resolve=macros.resolve_prompt_messages,
    )

    reasoning_on = bool((settings.get("reasoning_enabled_passes") or {}).get("editor", False))
    result = None
    async for ev in world_change_step(
        agent_client,
        base,
        settings=settings,
        entries=entries,
        worlds=[world],
        reply_text=assistant_msg["content"],
        writer_user_msg=user_msg["content"],
        exchange_text=f"{user_msg['content']}\n{assistant_msg['content']}",
        reasoning_on=reasoning_on,
    ):
        if ev["type"] == "done":
            result = ev["result"]

    if result is None or result.failed:
        raise db.OverlayStateConflict("re-evaluation did not produce a valid decision; the original proposal remains open")
    if result.is_empty:
        await retire()
        return None
    operations = split_by_world(result.operations).get(str(world_id), [])
    if not operations:
        await retire()
        return None
    return await supersede_proposal(
        changeset_id,
        {
            "world_id": world_id,
            "base_revision": int(world["content_revision"]),
            "summary": result.summary,
            "operations": operations,
            "source_user_message_id": int(user_msg["id"]),
            "source_assistant_message_id": int(assistant_msg["id"]),
            "source_conversation_id": conversation_id,
            "source_character_label": changeset.get("source_character_label", ""),
            "source_conversation_label": changeset.get("source_conversation_label", ""),
            "origin": "re_evaluate",
        },
    )
