"""The assistant's proposal store and Apply.

A proposal is server-owned: it holds the canonical target, the one field it changes, and the text the preview showed before and
after. Apply names proposals and nothing else, so a client cannot substitute arguments or after values, and the database
compares and writes each in one transaction. Proposals live in this process only; Orb runs one server process, and a restart
leaves every proposal expired.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ...database import (
    StaleWrite,
    get_character_card,
    get_conversation,
    get_message_by_id,
    save_message_edit,
    sync_conversations_for_card,
    update_character_card,
    update_lorebook_entry,
    update_user_persona,
)
from ...features.assistant import settled
from ..deps import ChatUnavailable, idle_chat_admission

MAX_PROPOSALS = 32
PROPOSAL_TTL_SECS = 15 * 60

ProposalKind = Literal["character", "lorebook_entry", "persona", "message"]
FIELD_LABELS = {
    "description": "Description",
    "personality": "Personality",
    "scenario": "Scenario",
    "first_mes": "First message",
    "mes_example": "Example messages",
    "creator_notes": "Creator notes",
    "name": "Name",
    "content": "Content",
    "tags": "Tags",
    "alternate_greetings": "Alternate greetings",
}


@dataclass(eq=False)
class Proposal:
    id: str
    kind: ProposalKind
    # The canonical target as a committed resource: ``{"kind": ..., <ids>}``.
    target: Mapping[str, Any]
    label: str
    name: str
    # The stored text the preview compares against, and the text Apply writes; a list field's whole list.
    before: str | list[str]
    after: str | list[str]
    # When the store forgets it: its last change plus the expiry, and once applied, that Apply plus the expiry.
    expires: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Set the moment the guarded write returns, and returned again to a repeated Apply.
    result: dict[str, Any] | None = None


_proposals: OrderedDict[str, Proposal] = OrderedDict()
_clock: Callable[[], float] = time.monotonic


def _clean() -> None:
    """Forget expired proposals, except one an Apply is still working on."""
    now = _clock()
    for proposal_id in [key for key, item in _proposals.items() if item.expires < now and not item.lock.locked()]:
        del _proposals[proposal_id]


def _evict() -> bool:
    """Forget the oldest proposal not yet applied, else the oldest applied one; never one an Apply is working on."""
    idle = [item for item in _proposals.values() if not item.lock.locked()]
    victim = next((item for item in idle if item.result is None), idle[0] if idle else None)
    if victim is None:
        return False
    del _proposals[victim.id]
    return True


def get_proposal(proposal_id: str) -> Proposal | None:
    _clean()
    return _proposals.get(proposal_id)


def store(
    kind: ProposalKind, target: Mapping[str, Any], label: str, name: str, before: str | list[str], after: str | list[str]
) -> Proposal:
    """Hold a new proposal to change field *name* of *target* from *before* to *after*, forgetting the oldest if full."""
    _clean()
    while len(_proposals) >= MAX_PROPOSALS and _evict():
        pass
    proposal = Proposal(
        secrets.token_hex(16), kind, dict(target), label, name, before, after, expires=_clock() + PROPOSAL_TTL_SECS
    )
    _proposals[proposal.id] = proposal
    return proposal


def is_open(proposal: Proposal) -> bool:
    """Whether *proposal* is still held and not yet applied, so an edit may still stack on it."""
    _clean()
    return _proposals.get(proposal.id) is proposal and proposal.result is None


def revise(proposal: Proposal, after: str | list[str]) -> None:
    """Replace what *proposal* writes, and keep it for a whole expiry period from now."""
    proposal.after, proposal.expires = after, _clock() + PROPOSAL_TTL_SECS


_MISSING = "What this proposal edits no longer exists."
_STALE = "That text changed after the preview, so nothing was applied. Ask again for a fresh preview."


class _Refused(Exception):
    """A verdict other than ``applied``: ``stale``, ``missing`` or ``busy``, with what the user is told."""

    def __init__(self, verdict: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.verdict = {"verdict": verdict, "message": message, **extra}


async def _write_message(proposal: Proposal) -> None:
    """The message edit: admitted under its chat's lane, compared and written with every invariant in one transaction."""
    cid = proposal.target["conversation_id"]
    # Apply is not a chat route, so the deletion fence on chat paths does not cover it; the admission does.
    try:
        async with idle_chat_admission(cid):
            original = await get_message_by_id(proposal.target["message_id"])
            if await get_conversation(cid) is None or original is None or original["conversation_id"] != cid:
                raise _Refused("missing", _MISSING)
            assert isinstance(proposal.after, str) and isinstance(proposal.before, str)
            if not await save_message_edit(original, proposal.after, expected=proposal.before):
                raise _Refused("missing", _MISSING)
    except ChatUnavailable as exc:
        # Nothing is recorded: either may pass, and the same proposal can be applied again.
        if exc.reason == "deleting":
            raise _Refused("busy", "That chat is being deleted. Nothing was applied.") from None
        raise _Refused("busy", "That chat is generating right now. Apply again once it has finished.") from None


async def _write(proposal: Proposal) -> None:
    """Compare the expected text and write the approved text in one transaction, through each target's own write path."""
    if proposal.kind == "message":
        return await _write_message(proposal)
    target, before, after = proposal.target, {proposal.name: proposal.before}, {proposal.name: proposal.after}
    if proposal.kind == "character":
        written: object = await update_character_card(target["character_id"], after, expected=before)
    elif proposal.kind == "lorebook_entry":
        written = await update_lorebook_entry(target["entry_id"], after, expected=before, world_id=target["world_id"])
    else:
        written = await update_user_persona(target["persona_id"], after, expected=before)
    if written is None:
        raise _Refused("missing", _MISSING)


async def _apply(proposal: Proposal) -> dict[str, Any]:
    """Write *proposal* once and keep the answer; a repeated Apply returns that answer without comparing again."""
    repeated = proposal.result is not None
    if proposal.result is None:
        try:
            await _write(proposal)
        except StaleWrite:
            raise _Refused("stale", _STALE, fields=[proposal.name]) from None
        proposal.result = {
            "verdict": "applied",
            "summary": f"Applied the edit to the {FIELD_LABELS[proposal.name].lower()} of {proposal.label}.",
            "resources": [dict(proposal.target)],
        }
        proposal.expires = _clock() + PROPOSAL_TTL_SECS
    if proposal.kind == "character":
        # What the card route does after its write, in its own commit: safe to repeat, so a repeated Apply runs it again, and
        # one that failed is retried until it succeeds.
        card = await get_character_card(proposal.target["character_id"])
        if card is not None:
            await sync_conversations_for_card(proposal.target["character_id"], card)
    return {**proposal.result, "repeated": True} if repeated else proposal.result


async def apply_proposals(proposal_ids: Sequence[str]) -> list[dict[str, Any]]:
    """One verdict per proposal, each compared and written on its own, under its own lock.

    Concurrent and repeated Applies of one proposal wait for the first and return its answer. Once admitted a write settles
    whole, through a dropped connection too: a cancelled await proves nothing about whether SQLite committed.
    """
    verdicts = []
    for proposal_id in dict.fromkeys(proposal_ids):
        proposal = get_proposal(proposal_id)
        if proposal is None:
            message = "This proposal has expired or is unknown to the server. Ask again."
            verdicts.append({"proposal_id": proposal_id, "verdict": "expired", "message": message})
            continue
        async with proposal.lock:
            try:
                verdict = await settled(_apply(proposal))
            except _Refused as refused:
                verdict = refused.verdict
        verdicts.append({"proposal_id": proposal_id, **verdict})
    return verdicts
