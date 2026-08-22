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

**At most one pending proposal per member.** An exchange stages against the sheet as
it stands, so two pending proposals for one member are necessarily derived from
the same base: applying either one makes the other unapplyable, and the user is
handed a pile of rows of which all but one can only 409. Regenerating a reply
re-runs the exchange and would stack another. So a new proposal *replaces* the
member's pending one in place, and the stage carries the replaced text forward
(see ``pipeline/sheet_update``) so the changes accumulate instead of competing.
The row keeps its id, which is also what lets an open review surface repaint
rather than grow.
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
# What the review surface asks for: what still needs a decision, plus what the
# apply already refused and therefore owes the user a reason for.
REVIEW_STATUSES = ("pending", "stale")


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
            # ``active = 1``: a tombstoned member still has a row (old messages keep
            # their names through it), but it is no longer in the scene and has no
            # sheet any turn will read. Without this the apply happily wrote onto a
            # member the user had removed, and the 409 this function exists to raise
            # was unreachable.
            "SELECT character_card_id, card_sheet_override FROM group_members "
            "WHERE id = ? AND conversation_id = ? AND active = 1",
            (member_id, conversation_id),
        )
    )
    if not rows:
        return None
    row = dict(rows[0])
    card = await get_character_card(row["character_card_id"]) if row["character_card_id"] else None
    return _private_sheet(card, row["card_sheet_override"])


async def get_pending_sheet_proposals(conversation_id: str) -> dict[str, MemberSheetProposalRow]:
    """The scene's pending proposals keyed by member — at most one each.

    Read by the staging pass so a fresh exchange can carry an undecided proposal
    forward rather than competing with it.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM member_sheet_proposals WHERE conversation_id = ? AND status = 'pending' ORDER BY id",
            (conversation_id,),
        )
    return {str(row["member_id"]): cast(MemberSheetProposalRow, dict(row)) for row in rows}


async def create_sheet_proposals(proposals: Sequence[Mapping[str, Any]]) -> list[MemberSheetProposalRow]:
    """Stage proposals, one per member. Applies nothing.

    Written in one transaction so an exchange's proposals arrive together — a review
    surface that painted half of them would read as the pass having judged only
    half the cast.

    An **upsert**: a member with an undecided proposal has that row rewritten
    rather than a second one added beside it. See the module docstring for why
    two pending rows for one member cannot both be honoured.
    """
    if not proposals:
        return []
    now = datetime.now(UTC).isoformat()
    ids: list[int] = []
    async with immediate_tx() as db:
        for proposal in proposals:
            conversation_id = str(proposal["conversation_id"])
            member_id = str(proposal["member_id"])
            values = (
                str(proposal.get("exchange_id") or ""),
                str(proposal.get("base_sheet") or ""),
                str(proposal["proposed_sheet"]),
                str(proposal.get("summary") or ""),
                now,
            )
            existing = list(
                await db.execute_fetchall(
                    "SELECT id FROM member_sheet_proposals "
                    "WHERE conversation_id = ? AND member_id = ? AND status = 'pending' ORDER BY id",
                    (conversation_id, member_id),
                )
            )
            if existing:
                # Keep the oldest row's id and retire any duplicates a build
                # before this rule left behind, so the invariant holds from here
                # on without a migration to repair history.
                keep = int(existing[0]["id"])
                await db.execute(
                    """UPDATE member_sheet_proposals
                       SET exchange_id = ?, base_sheet = ?, proposed_sheet = ?, summary = ?, created_at = ?,
                           status = 'pending', decided_at = NULL
                       WHERE id = ?""",
                    (*values, keep),
                )
                for row in existing[1:]:
                    await db.execute(
                        "UPDATE member_sheet_proposals SET status = 'rejected', decided_at = ? WHERE id = ?",
                        (now, int(row["id"])),
                    )
                ids.append(keep)
                continue
            cur = await db.execute(
                """INSERT INTO member_sheet_proposals
                   (conversation_id, member_id, exchange_id, base_sheet, proposed_sheet, summary, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (conversation_id, member_id, *values),
            )
            if cur.lastrowid is not None:
                ids.append(int(cur.lastrowid))
    if not ids:
        return []
    async with get_db() as db:
        placeholders = ",".join("?" for _ in ids)
        rows = await db.execute_fetchall(
            f"SELECT * FROM member_sheet_proposals WHERE id IN ({placeholders}) ORDER BY id",  # nosec B608 -- ints
            tuple(ids),
        )
    return [cast(MemberSheetProposalRow, dict(row)) for row in rows]


async def get_sheet_proposals(
    conversation_id: str, *, statuses: Sequence[str] | None = REVIEW_STATUSES
) -> list[MemberSheetProposalRow]:
    """The conversation's proposals, newest first. ``statuses=None`` returns all.

    Defaults to the **review set** rather than to ``pending`` alone. A ``stale``
    proposal is one the apply refused, and it is precisely the row the user has
    to see an explanation on — fetching only ``pending`` made it vanish from the
    surface the moment it was refused, which is the opposite of reporting the
    refusal.
    """
    sql = "SELECT * FROM member_sheet_proposals WHERE conversation_id = ?"
    args: tuple[Any, ...] = (conversation_id,)
    if statuses is not None:
        if not statuses:
            return []
        sql += f" AND status IN ({','.join('?' for _ in statuses)})"  # nosec B608 -- fixed vocabulary
        args += tuple(statuses)
    async with get_db() as db:
        rows = await db.execute_fetchall(sql + " ORDER BY id DESC", args)
    return [cast(MemberSheetProposalRow, dict(row)) for row in rows]


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
