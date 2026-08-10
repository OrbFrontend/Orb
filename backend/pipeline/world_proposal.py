"""
world_proposal.py — the Dynamic Worlds stage of a turn.

Sits between the orchestrator and ``passes/world_change.py``: decides whether
the stage runs at all, re-reads the target World, drives the forced tool call,
and parks the validated result on :class:`TurnState` for persistence to stage.

Nothing here writes to the World. The stage produces a *pending* proposal;
applying it is always a separate, reviewed action (``features/lorebook``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from .. import database as db
from ..features.lorebook import dynamic_enabled, stage_proposal
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


async def world_proposal_stage(
    cfg: _PipelineConfig,
    state: TurnState,
    *,
    settings: Mapping[str, Any],
    turn: WorldProposalTurn,
    kv_tracker=None,
) -> AsyncIterator[dict]:
    """Run the proposal step and record its result on *state*.

    Yields the step's reasoning under the ``editor`` pass label (it shares the
    agent lane and the post-turn placement with the editor's own sub-steps), and
    appends the parsed tool call to ``state.calls`` so the inspector shows it
    alongside the rest of the turn.

    Every failure path is contained: a missing World, a model that calls nothing,
    a call that validates to zero operations, and an outright transport error all
    leave ``state.world_proposal`` as ``None``. This runs immediately before the
    turn's ``_result``, so it must never be able to cost the user their reply.
    """
    try:
        world = await db.get_world(turn.world_id)
        if world is None:
            return
        # The turn-level tool blob was fixed when generation began, but the
        # user's opt-in is authoritative at the moment this optional call would
        # run. A cross-tab disable during a long writer/editor pass must stop the
        # proposal even though its schema remains harmlessly present in the blob.
        if not dynamic_enabled(world):
            return
        entries = await db.get_lorebook_entries(turn.world_id)
    except Exception:
        logger.exception(
            "World-change stage could not load world %s; proposing nothing",
            turn.world_id,
        )
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

    state.world_proposal = {
        "world_id": turn.world_id,
        "base_revision": int(world["content_revision"]),
        "summary": result.summary,
        "operations": result.operations,
        "source_conversation_id": turn.conversation_id,
        "source_character_label": turn.character_label,
        "source_conversation_label": turn.conversation_label,
    }


async def reevaluate_changeset(changeset: Mapping[str, Any]):
    """Re-run the proposal step for a stale changeset against the current World.

    The replacement for automatic rebasing. Two changes that touch different
    entries can still contradict each other in meaning, so a proposal that lost
    its revision race is not replayed — it is re-derived from its stored source
    messages against the World as it now stands, and comes back as a fresh
    pending changeset (linked via ``supersedes_changeset_id``).

    Returns the new changeset, or ``None`` when re-evaluation is not possible
    (the source messages are gone, the World is gone, or the model proposed
    nothing this time). The stale original is left as-is either way — the caller
    decides what to tell the user.
    """
    asst_id = changeset.get("source_assistant_message_id")
    user_id = changeset.get("source_user_message_id")
    if asst_id is None:
        return None
    assistant_msg = await db.get_message_by_id(int(asst_id))
    user_msg = await db.get_message_by_id(int(user_id)) if user_id is not None else None
    if assistant_msg is None or user_msg is None:
        return None

    world_id = changeset["world_id"]
    world = await db.get_world(world_id)
    if world is None:
        return None
    entries = await db.get_lorebook_entries(world_id)

    settings = await db.get_settings()
    conversation_id = assistant_msg["conversation_id"]
    conv = await db.get_conversation(conversation_id)
    if conv is None:
        return None
    # History up to (but not including) the source exchange: the exchange itself
    # is the step's trailing, exactly as it is during a live turn.
    parent_id = user_msg.get("parent_id")
    history = (
        await db.get_path_to_leaf(conversation_id, parent_id)
        if parent_id is not None
        else []
    )
    prefix = await build_offturn_prefix(
        conversation_id, history, settings, lane="agent"
    )
    if not prefix:
        return None

    _, persona = await resolve_card_and_persona(conv, settings)
    macros, _ = persona_macros(
        settings, conv["character_name"], persona, seed=conversation_macro_seed(conv)
    )
    client = client_from_settings(settings)
    agent_client, model = agent_lane_from_settings(settings, writer_client=client)
    base = CachedBase(
        prefix=tuple(prefix),
        # The same enabled blob a live turn on this conversation would send, so
        # the re-evaluation extends that conversation's warm cached bottom
        # instead of paying for a fresh prefix.
        tools=tuple(
            enabled_schemas(
                {**(settings.get("enabled_tools") or {}), "propose_world_changes": True}
            )
        ),
        model=model,
        resolve=macros.resolve_prompt_messages,
    )

    reasoning_on = bool(
        (settings.get("reasoning_enabled_passes") or {}).get("editor", False)
    )
    result = None
    async for ev in world_change_step(
        agent_client,
        base,
        settings=settings,
        entries=entries,
        reply_text=assistant_msg["content"],
        writer_user_msg=user_msg["content"],
        exchange_text=f"{user_msg['content']}\n{assistant_msg['content']}",
        reasoning_on=reasoning_on,
    ):
        if ev["type"] == "done":
            result = ev["result"]

    if result is None or result.is_empty:
        return None
    return await stage_proposal(
        world_id=world_id,
        base_revision=int(world["content_revision"]),
        summary=result.summary,
        operations=result.operations,
        source_user_message_id=int(user_msg["id"]),
        source_assistant_message_id=int(assistant_msg["id"]),
        source_conversation_id=conversation_id,
        source_character_label=changeset.get("source_character_label", ""),
        source_conversation_label=changeset.get("source_conversation_label", ""),
        supersedes_changeset_id=int(changeset["id"]),
        origin="re_evaluate",
    )
