"""The review queue for scene-local sheet updates: stage, apply, reject.

Sits beside ``group_members`` because it writes exactly one of its columns
(``card_sheet_override``) and reads the same card fallback to decide whether it
may. Kept out of that module so the roster's own resolution stays the small,
hot, every-prefix-reads-it thing it is.

**One writer wins.** ``base_sheet`` is what ``worlds.content_revision`` is to a
Dynamic Worlds changeset: the apply takes ``BEGIN IMMEDIATE``, re-resolves the
member's current effective sheet, and refuses when it no longer matches what the
proposal was derived from. The proposal is marked ``stale`` rather than forced
through, because a hand edit and a model's edit can contradict each other in
meaning even when both look reasonable — the same reason nothing here rebases.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from ..connection import get_db, immediate_tx
from ..models import MemberSheetProposalRow
from .character_cards import get_character_card
from .group_members import _private_sheet

PROPOSAL_STATUSES = ("pending", "applied", "rejected", "stale")


class SheetProposalConflict(RuntimeError):
    """The member's sheet moved since the proposal was derived from it.

    Raised by :func:`apply_sheet_proposal` only. The route turns it into a 409,
    matching the changeset apply — there is no force-apply here either.
    """


async def _effective_sheet(db, conversation_id: str, member_id: str) -> str | None:
    """The member's current sheet as ``resolve_cast`` would render it, or ``None``.

    Read through the *same* ``_private_sheet`` the turn uses rather than
    comparing the raw column, so a proposal derived from card text (override
    ``NULL``) is checked against that card text and not against ``""``. Two
    resolvers here would mean the staleness check answered a different question
    from the one the prompt asked.
    """
    rows = list(
        await db.execute_fetchall(
            "SELECT character_card_id, card_sheet_override FROM group_members WHERE id = ? AND conversation_id = ?",
            (member_id, conversation_id),
        )
    )
    if not rows:
        return None
    row = dict(rows[0])
    card = await get_character_card(row["character_card_id"]) if row["character_card_id"] else None
    return _private_sheet(card, row["card_sheet_override"])


async def create_sheet_proposals(proposals: Sequence[Mapping[str, Any]]) -> list[MemberSheetProposalRow]:
    """Persist staged proposals, one row each. Applies nothing.

    Written in one transaction so a beat's proposals arrive together — a review
    surface that painted half of them would read as the pass having judged only
    half the cast.
    """
    if not proposals:
        return []
    now = datetime.now(UTC).isoformat()
    ids: list[int] = []
    async with immediate_tx() as db:
        for proposal in proposals:
            cur = await db.execute(
                """INSERT INTO member_sheet_proposals
                   (conversation_id, member_id, beat_id, base_sheet, proposed_sheet, summary, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    str(proposal["conversation_id"]),
                    str(proposal["member_id"]),
                    str(proposal.get("beat_id") or ""),
                    str(proposal.get("base_sheet") or ""),
                    str(proposal["proposed_sheet"]),
                    str(proposal.get("summary") or ""),
                    now,
                ),
            )
            if cur.lastrowid is not None:
                ids.append(int(cur.lastrowid))
    async with get_db() as db:
        placeholders = ",".join("?" for _ in ids)
        rows = await db.execute_fetchall(
            f"SELECT * FROM member_sheet_proposals WHERE id IN ({placeholders}) ORDER BY id",  # nosec B608 -- ints
            tuple(ids),
        )
    return [cast(MemberSheetProposalRow, dict(row)) for row in rows]


async def get_sheet_proposals(conversation_id: str, *, status: str | None = "pending") -> list[MemberSheetProposalRow]:
    """The conversation's proposals, newest first. ``status=None`` returns all."""
    sql = "SELECT * FROM member_sheet_proposals WHERE conversation_id = ?"
    args: tuple[Any, ...] = (conversation_id,)
    if status is not None:
        sql += " AND status = ?"
        args += (status,)
    async with get_db() as db:
        rows = await db.execute_fetchall(sql + " ORDER BY id DESC", args)
    return [cast(MemberSheetProposalRow, dict(row)) for row in rows]


async def get_sheet_proposal(proposal_id: int, *, conversation_id: str) -> MemberSheetProposalRow | None:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT * FROM member_sheet_proposals WHERE id = ? AND conversation_id = ?",
                (proposal_id, conversation_id),
            )
        )
    return cast(MemberSheetProposalRow, dict(rows[0])) if rows else None


async def apply_sheet_proposal(proposal_id: int, *, conversation_id: str) -> MemberSheetProposalRow:
    """Write the proposed sheet onto the member, in one guarded transaction.

    Refuses a proposal that is not ``pending`` and one whose member's sheet has
    moved since it was derived. The refusal is decided inside the transaction
    and *reported* outside it: ``immediate_tx`` rolls back on any exception, so
    raising in place would take the ``stale`` mark down with it and the review
    row would keep offering an apply that can only be refused again.
    """
    conflict = ""
    proposal: dict[str, Any] = {}
    async with immediate_tx() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT * FROM member_sheet_proposals WHERE id = ? AND conversation_id = ?",
                (proposal_id, conversation_id),
            )
        )
        if not rows:
            conflict = "That sheet proposal no longer exists."
        else:
            proposal = dict(rows[0])
            current = await _effective_sheet(db, conversation_id, str(proposal["member_id"]))
            now = datetime.now(UTC).isoformat()
            if proposal["status"] != "pending":
                conflict = f"That sheet proposal is already {proposal['status']}."
            elif current is None:
                conflict = "That cast member is no longer in the scene."
            elif current != proposal["base_sheet"]:
                conflict = "That sheet has changed since this update was proposed."
                await db.execute(
                    "UPDATE member_sheet_proposals SET status = 'stale', decided_at = ? WHERE id = ?",
                    (now, proposal_id),
                )
            else:
                await db.execute(
                    "UPDATE group_members SET card_sheet_override = ? WHERE id = ? AND conversation_id = ?",
                    (proposal["proposed_sheet"], proposal["member_id"], conversation_id),
                )
                await db.execute(
                    "UPDATE member_sheet_proposals SET status = 'applied', decided_at = ? WHERE id = ?",
                    (now, proposal_id),
                )
                proposal.update(status="applied", decided_at=now)
    if conflict:
        raise SheetProposalConflict(conflict)
    return cast(MemberSheetProposalRow, proposal)


async def reject_sheet_proposal(proposal_id: int, *, conversation_id: str) -> MemberSheetProposalRow:
    """Retire a proposal without writing anything.

    Accepts a ``stale`` one as well as a ``pending`` one: staleness is a reason
    the apply refused, not a state the user should be stuck reviewing.
    """
    conflict = ""
    proposal: dict[str, Any] = {}
    async with immediate_tx() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT * FROM member_sheet_proposals WHERE id = ? AND conversation_id = ?",
                (proposal_id, conversation_id),
            )
        )
        if not rows:
            conflict = "That sheet proposal no longer exists."
        else:
            proposal = dict(rows[0])
            if proposal["status"] not in ("pending", "stale"):
                conflict = f"That sheet proposal is already {proposal['status']}."
            else:
                now = datetime.now(UTC).isoformat()
                await db.execute(
                    "UPDATE member_sheet_proposals SET status = 'rejected', decided_at = ? WHERE id = ?",
                    (now, proposal_id),
                )
                proposal.update(status="rejected", decided_at=now)
    if conflict:
        raise SheetProposalConflict(conflict)
    return cast(MemberSheetProposalRow, proposal)
