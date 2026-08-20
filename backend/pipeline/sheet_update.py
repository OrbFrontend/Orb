"""sheet_update.py — the scene-local sheet stage of a group exchange.

The sibling of :mod:`world_proposal`, and the same shape: decides whether the
stage runs at all, re-reads its targets, drives one forced tool call per target,
and stages the validated results for the user to review. Two differences, both
from what a sheet is:

**One call per member, never batched.** ``features/cards/sheet_update`` owns
that rule and states why. This module is what honours it — the loop below is
per member, and each iteration sends that member's sheet and no other's.

**It runs once per exchange, not once per speaker.** ``run_exchange_final`` gates it in
the orchestrator, and the exchange driver hands it the exchange's persisted replies; the
running speaker's own draft is appended here, because it is not persisted until
after this returns. Cast-wide would bill a call per member per exchange to tell a
silent member that nothing happened to them, so the targets are the members that
actually spoke.

**An undecided proposal is carried forward, not competed with.** A member with a
pending proposal has this exchange's call built on *that* text rather than on the
member's stored sheet, so two exchanges of drift accumulate into one reviewable
sheet instead of two mutually-exclusive ones (``database.queries.member_sheets``
states the rule; this is the half that makes the replacement lossless). The row
staged still records the member's *stored* sheet as ``base_sheet``, because that
is what the apply has to re-check against. A pending proposal whose base no
longer matches the stored sheet is ignored: the user hand-edited underneath it,
and resurrecting text they overwrote is the one thing staging must not do.

Every failure path leaves the exchange untouched and stages nothing. This runs
immediately before the turn's ``_result``, so a bookkeeping step must never be
able to cost the user their reply — the same rule the world stage states, held
here at both levels: one member's failed call does not drop another's proposal.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from .. import database as db
from ..features.cards import (
    SHEET_TOOL_NAME,
    SheetUpdateUnavailable,
    build_exchange_transcript,
    propose_sheet_update,
)
from .state import SheetUpdateTurn, TurnState, _PipelineConfig

logger = logging.getLogger(__name__)


def _exchange_transcript(turn: SheetUpdateTurn, state: TurnState, speaker_name: str) -> str:
    """The exchange as one block: the user's message, the persisted replies, this draft."""
    lines: list[tuple[str, str]] = []
    if turn.user_message.strip():
        lines.append(("User", turn.user_message))
    lines.extend(turn.lines)
    if state.resp_text.strip():
        lines.append((speaker_name or "Speaker", state.resp_text))
    return build_exchange_transcript(lines)


async def sheet_update_stage(
    cfg: _PipelineConfig,
    state: TurnState,
    *,
    turn: SheetUpdateTurn,
) -> AsyncIterator[dict]:
    """Propose sheet updates for the members this exchange touched, and stage them.

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
        # member's sheet as it stands *now* -- after the exchange's own latency, and
        # after any hand edit made while the exchange was generating.
        cast = await db.resolve_cast(conv) if conv else None
    except Exception:
        logger.exception("Sheet-update stage could not resolve the cast for %s; proposing nothing", turn.conversation_id)
        return
    if cast is None or not cast.grouped:
        return
    by_id = {member.member_id: member for member in cast.members}
    # ``dict.fromkeys`` and not ``set``: a member that spoke twice this exchange is
    # one target, and the order the driver sent is the order the exchange ran in.
    targets = [by_id[mid] for mid in dict.fromkeys(turn.member_ids) if mid in by_id]
    if not targets:
        return
    try:
        pending = await db.get_pending_sheet_proposals(turn.conversation_id)
    except Exception:
        # A queue that cannot be read costs the carry-forward, not the exchange: the
        # pass falls back to staging against the stored sheet, which is what it
        # did before the two were chained.
        logger.exception("Could not read pending sheet proposals for %s; staging without carry-forward", turn.conversation_id)
        pending = {}

    transcript = _exchange_transcript(turn, state, turn.speaker_name)
    if not transcript.strip():
        return

    # A standalone two-message call on the agent lane's client, *not* an
    # extension of its cached base -- the same posture the public-profile
    # drafter takes. A per-member call carrying the whole scene prefix would
    # bill the cast's context once per touched member for a question that only
    # needs one sheet and one exchange.
    client, model = cfg.agent_lane.client, cfg.agent_lane.base.model
    staged: list[dict] = []
    for member in targets:
        # The sheet this exchange reasons *from*: the member's undecided proposal when
        # it still applies, otherwise what the member actually reads today. Only a
        # proposal whose base still matches is carried — a mismatch means the user
        # edited the sheet by hand since, and their text wins.
        prior = pending.get(member.member_id)
        carried = (
            str(prior["proposed_sheet"])
            if prior is not None and str(prior["base_sheet"]) == member.private_sheet
            else member.private_sheet
        )
        # A member with no sheet at all has nothing to carry forward, and a
        # proposal built from "" would be the model inventing a character rather
        # than recording a change to one.
        if not carried.strip():
            continue
        try:
            update = await propose_sheet_update(
                client,
                model,
                member_name=member.name,
                sheet=carried,
                transcript=transcript,
            )
        except SheetUpdateUnavailable as exc:
            logger.info("Sheet update for %s produced nothing usable: %s", member.name, exc)
            continue
        except Exception:
            logger.exception("Sheet update call failed for member %s; the rest of the exchange is unaffected", member.member_id)
            continue
        # On state.calls for the same reason the world stage puts its call there:
        # the pass bills one request per touched member, and a billed call the
        # inspector and the turn log never show is a cost with no record.
        state.calls.append(
            {
                "name": SHEET_TOOL_NAME,
                "arguments": {
                    "member": member.name,
                    "changed": update is not None,
                    **({"summary": update["summary"]} if update else {}),
                },
            }
        )
        if update is None:
            continue
        staged.append(
            {
                "conversation_id": turn.conversation_id,
                "member_id": member.member_id,
                "exchange_id": turn.exchange_id,
                # The member's *stored* sheet, not the text the call reasoned
                # from: this is the value the apply re-checks against, and by
                # review time the member may have been hand-edited, which is
                # exactly the case this has to detect. When a pending proposal
                # was carried forward the two differ, and it is this one — the
                # one an apply can actually match — that has to be recorded.
                "base_sheet": member.private_sheet,
                "proposed_sheet": update["sheet"],
                "summary": update["summary"],
            }
        )
    if not staged:
        return
    # Persisted here rather than at the ``_result`` boundary the world proposals
    # ride: a sheet proposal names an *exchange*, which is already known, so it has
    # nothing to wait for the assistant row's id for.
    try:
        rows = await db.create_sheet_proposals(staged)
        logger.info("Staged %d sheet proposal(s) for exchange %s", len(rows), turn.exchange_id)
    except Exception:
        logger.exception(
            "Failed to stage %d sheet proposal(s) for exchange %s; the reply is unaffected", len(staged), turn.exchange_id
        )
