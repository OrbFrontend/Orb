"""sheet_update.py — the scene-local sheet stage of a group beat.

The sibling of :mod:`world_proposal`, and the same shape: decides whether the
stage runs at all, re-reads its targets, drives one forced tool call per target,
and stages the validated results for the user to review. Two differences, both
from what a sheet is:

**One call per member, never batched.** ``features/cards/sheet_update`` owns
that rule and states why. This module is what honours it — the loop below is
per member, and each iteration sends that member's sheet and no other's.

**It runs once per beat, not once per speaker.** ``run_beat_final`` gates it in
the orchestrator, and the beat driver hands it the beat's persisted replies; the
running speaker's own draft is appended here, because it is not persisted until
after this returns. Cast-wide would bill a call per member per beat to tell a
silent member that nothing happened to them, so the targets are the members that
actually spoke.

Every failure path leaves the beat untouched and stages nothing. This runs
immediately before the turn's ``_result``, so a bookkeeping step must never be
able to cost the user their reply — the same rule the world stage states, held
here at both levels: one member's failed call does not drop another's proposal.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from .. import database as db
from ..features.cards import (
    SheetUpdateUnavailable,
    build_beat_transcript,
    propose_sheet_update,
)
from .state import SheetUpdateTurn, TurnState, _PipelineConfig

logger = logging.getLogger(__name__)


def _beat_transcript(turn: SheetUpdateTurn, state: TurnState, speaker_name: str) -> str:
    """The beat as one block: the user's message, the persisted replies, this draft."""
    lines: list[tuple[str, str]] = []
    if turn.user_message.strip():
        lines.append(("User", turn.user_message))
    lines.extend(turn.lines)
    if state.resp_text.strip():
        lines.append((speaker_name or "Speaker", state.resp_text))
    return build_beat_transcript(lines)


async def sheet_update_stage(
    cfg: _PipelineConfig,
    state: TurnState,
    *,
    settings: Mapping[str, Any],
    turn: SheetUpdateTurn,
) -> AsyncIterator[dict]:
    """Propose sheet updates for the members this beat touched, and stage them.

    An async generator with no yields today, for symmetry with
    ``world_proposal_stage`` and so a future reasoning passthrough is an
    additive change at the call site rather than a signature change. The
    orchestrator drives it the same way.
    """
    if False:  # pragma: no cover - keeps the stage an async generator
        yield {}
    try:
        conv = await db.get_conversation(turn.conversation_id)
        # Re-resolved here rather than read off the turn: the sheet a proposal is
        # derived from is also the value its apply re-checks, so it has to be the
        # member's sheet as it stands *now* -- after the beat's own latency, and
        # after any hand edit made while the beat was generating.
        cast = await db.resolve_cast(conv) if conv else None
    except Exception:
        logger.exception("Sheet-update stage could not resolve the cast for %s; proposing nothing", turn.conversation_id)
        return
    if cast is None or not cast.grouped:
        return
    by_id = {member.member_id: member for member in cast.members}
    # ``dict.fromkeys`` and not ``set``: a member that spoke twice this beat is
    # one target, and the order the driver sent is the order the beat ran in.
    targets = [by_id[mid] for mid in dict.fromkeys(turn.member_ids) if mid in by_id]
    if not targets:
        return

    transcript = _beat_transcript(turn, state, turn.speaker_name)
    if not transcript.strip():
        return

    # A standalone two-message call on the agent lane's client, *not* an
    # extension of its cached base -- the same posture the public-profile
    # drafter takes. A per-member call carrying the whole scene prefix would
    # bill the cast's context once per touched member for a question that only
    # needs one sheet and one beat.
    client, model = cfg.agent_lane.client, cfg.agent_lane.base.model
    staged: list[dict] = []
    for member in targets:
        # A member with no sheet at all has nothing to carry forward, and a
        # proposal built from "" would be the model inventing a character rather
        # than recording a change to one.
        if not member.private_sheet.strip():
            continue
        try:
            update = await propose_sheet_update(
                client,
                model,
                member_name=member.name,
                sheet=member.private_sheet,
                transcript=transcript,
            )
        except SheetUpdateUnavailable as exc:
            logger.info("Sheet update for %s produced nothing usable: %s", member.name, exc)
            continue
        except Exception:
            logger.exception("Sheet update call failed for member %s; the rest of the beat is unaffected", member.member_id)
            continue
        if update is None:
            continue
        staged.append(
            {
                "conversation_id": turn.conversation_id,
                "member_id": member.member_id,
                "beat_id": turn.beat_id,
                # The sheet the proposal was derived from, and the value the
                # apply re-checks against. Stored rather than recomputed: by
                # review time the member may have been hand-edited, which is
                # exactly the case this has to detect.
                "base_sheet": member.private_sheet,
                "proposed_sheet": update["sheet"],
                "summary": update["summary"],
            }
        )
    if not staged:
        return
    # Persisted here rather than at the ``_result`` boundary the world proposals
    # ride: a sheet proposal names a *beat*, which is already known, so it has
    # nothing to wait for the assistant row's id for.
    try:
        rows = await db.create_sheet_proposals(staged)
        logger.info("Staged %d sheet proposal(s) for beat %s", len(rows), turn.beat_id)
    except Exception:
        logger.exception("Failed to stage %d sheet proposal(s) for beat %s; the reply is unaffected", len(staged), turn.beat_id)
