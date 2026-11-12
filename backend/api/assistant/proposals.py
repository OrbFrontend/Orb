"""The assistant's proposal store: Apply, Undo, the user's own revision of a proposed text, and what earlier requests left pending.

A proposal is server-owned: it holds the canonical target, the one field it changes (or that it deletes the target), and the
text the preview showed before and after. Apply and Undo name proposals and nothing else, so a client cannot substitute a
target, and the database compares and writes each in one transaction. The one text a client may send is the user's own
revision of a pending edit, which replaces what Apply writes and is shown again before it is applied. Proposals live in this
process only; Orb runs one server process, and a restart leaves every proposal expired.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import HTTPException

from ...core.macros import resolve_inline
from ...database import (
    StaleWrite,
    delete_character_card,
    delete_phrase_group,
    delete_user_persona,
    delete_world,
    get_character_card,
    get_conversation,
    get_document,
    get_lorebook_entry,
    get_message_by_id,
    get_settings,
    get_user_persona,
    get_world,
    save_message_edit,
    sync_conversations_for_card,
    update_character_card,
    update_lorebook_entry,
    update_settings,
    update_user_persona,
)
from ...features import lorebook
from ...features.assistant import settled
from ..deletions import delete_chat, delete_document_settled, delete_message_subtree
from ..deps import ChatUnavailable, conversation_busy, idle_chat_admission, idle_chats_guard
from .targets import clip

MAX_PROPOSALS = 32
PROPOSAL_TTL_SECS = 15 * 60
# How much of each pending text a later request is shown, and of all of them together.
_PENDING_TEXT_CHARS = 1500
_PENDING_NOTE_CHARS = 6000

ProposalKind = Literal["character", "lorebook_entry", "persona", "message", "world", "conversation", "document", "phrase_group"]
ProposalAction = Literal["edit", "delete"]
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
    "delete": "Delete",
}


@dataclass(eq=False)
class Proposal:
    id: str
    kind: ProposalKind
    # The canonical target as a committed resource: ``{"kind": ..., <ids>}``.
    target: Mapping[str, Any]
    label: str
    name: str
    # The stored text the preview compares against, and the text Apply writes; a list field's whole list. A delete holds what
    # it removes, as the user reads it, before, and nothing after.
    before: str | list[str]
    after: str | list[str]
    # When the store forgets it: its last change plus the expiry, and once applied, that Apply plus the expiry.
    expires: float
    action: ProposalAction = "edit"
    # A delete's extra word, such as whether a character's chats go with it.
    options: Mapping[str, Any] = field(default_factory=dict)
    # The proposal of an earlier request this one replaced; the store forgot that one when this one was made.
    replaces: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Set the moment the guarded write returns, and returned again to a repeated Apply; cleared by an Undo.
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
    kind: ProposalKind,
    target: Mapping[str, Any],
    label: str,
    name: str,
    before: str | list[str],
    after: str | list[str],
    *,
    action: ProposalAction = "edit",
    options: Mapping[str, Any] | None = None,
    replaces: Proposal | None = None,
) -> Proposal:
    """Hold a new proposal to change field *name* of *target* from *before* to *after*, forgetting the oldest if full.

    *replaces* is an earlier request's proposal for the same change, which the store forgets now: its preview is superseded.
    """
    _clean()
    if replaces is not None and _proposals.get(replaces.id) is replaces and not replaces.lock.locked():
        del _proposals[replaces.id]
    while len(_proposals) >= MAX_PROPOSALS and _evict():
        pass
    proposal = Proposal(
        secrets.token_hex(16),
        kind,
        dict(target),
        label,
        name,
        before,
        after,
        expires=_clock() + PROPOSAL_TTL_SECS,
        action=action,
        options=dict(options or {}),
        replaces=replaces.id if replaces is not None else "",
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


def open_proposals(proposal_ids: Sequence[str]) -> list[Proposal]:
    """The proposals *proposal_ids* name that are still held and not applied: what earlier requests left for the user."""
    held = (get_proposal(proposal_id) for proposal_id in dict.fromkeys(proposal_ids))
    return [proposal for proposal in held if proposal is not None and proposal.result is None]


def change_label(proposal: Proposal) -> str:
    """What *proposal* does, as a sentence names it: "the description of character "Mara"", "deleting world "Ashfall"."""
    if proposal.action == "delete":
        return f"deleting {proposal.label}"
    return f"the {FIELD_LABELS[proposal.name].lower()} of {proposal.label}"


def preview(proposal: Proposal) -> dict[str, Any]:
    """The preview the browser shows with Apply and Cancel; a later one with the same id, or naming it in ``replaces``,
    takes its place."""
    data: dict[str, Any] = {
        "proposal_id": proposal.id,
        "kind": proposal.kind,
        "action": proposal.action,
        "target": dict(proposal.target),
        "label": proposal.label,
        "fields": [
            {"field": proposal.name, "label": FIELD_LABELS[proposal.name], "before": proposal.before, "after": proposal.after}
        ],
        "expires_in_s": PROPOSAL_TTL_SECS,
    }
    if proposal.replaces:
        data["replaces"] = proposal.replaces
    return data


def pending_note(held: Sequence[Proposal]) -> str:
    """The note a request carries about what earlier requests proposed and the user has not applied yet.

    Each pending text is shown so a further edit can build on it, clipped like a read; the server's own copy, never the
    browser's.
    """
    if not held:
        return ""
    lines: list[str] = []
    room = _PENDING_NOTE_CHARS
    for proposal in held:
        if proposal.action == "delete":
            lines.append(f"- Deleting {proposal.label}.")
            continue
        after = proposal.after
        if isinstance(after, list):
            lines.append(f"- An edit to {change_label(proposal)}; its pending list: {clip(after, room)}")
        else:
            shown = clip(after, max(0, min(_PENDING_TEXT_CHARS, room)))
            lines.append(f"- An edit to {change_label(proposal)} ({len(after):,} characters); its pending text:\n{shown}")
        room -= len(lines[-1])
    return (
        "[Proposed in earlier requests and still waiting for the user's Apply. Reads show the stored text, not these. A further "
        "edit to one of these fields builds on its pending text and replaces it on the user's card. Do not propose any of "
        "them again. Anything proposed earlier and not listed was applied, cancelled or has expired.]\n" + "\n".join(lines)
    )


_MISSING = "What this proposal changes no longer exists."
_STALE = "That text changed after the preview, so nothing was applied. Ask again for a fresh preview."
_BUSY = "That chat is generating right now. Apply again once it has finished."


class _Refused(Exception):
    """A verdict other than ``applied``: ``stale``, ``missing`` or ``busy``, with what the user is told."""

    def __init__(self, verdict: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.verdict = {"verdict": verdict, "message": message, **extra}


async def _write_message(proposal: Proposal, expected: str, text: str) -> None:
    """The message edit: admitted under its chat's lane, compared and written with every invariant in one transaction."""
    cid = proposal.target["conversation_id"]
    # Apply is not a chat route, so the deletion fence on chat paths does not cover it; the admission does.
    try:
        async with idle_chat_admission(cid):
            original = await get_message_by_id(proposal.target["message_id"])
            if await get_conversation(cid) is None or original is None or original["conversation_id"] != cid:
                raise _Refused("missing", _MISSING)
            if not await save_message_edit(original, text, expected=expected):
                raise _Refused("missing", _MISSING)
    except ChatUnavailable as exc:
        # Nothing is recorded: either may pass, and the same proposal can be applied again.
        if exc.reason == "deleting":
            raise _Refused("busy", "That chat is being deleted. Nothing was applied.") from None
        raise _Refused("busy", _BUSY) from None


async def _write(proposal: Proposal, expected: str | list[str], value: str | list[str]) -> None:
    """Compare *expected* and write *value* in one transaction, through each target's own write path."""
    if proposal.kind == "message":
        assert isinstance(expected, str) and isinstance(value, str)
        return await _write_message(proposal, expected, value)
    target, before, after = proposal.target, {proposal.name: expected}, {proposal.name: value}
    if proposal.kind == "character":
        written: object = await update_character_card(target["character_id"], after, expected=before)
    elif proposal.kind == "lorebook_entry":
        written = await update_lorebook_entry(target["entry_id"], after, expected=before, world_id=target["world_id"])
    else:
        written = await update_user_persona(target["persona_id"], after, expected=before)
    if written is None:
        raise _Refused("missing", _MISSING)


async def _delete_character(target: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    try:
        return await delete_character_card(
            target["character_id"], bool(options.get("delete_chats")), idle_guard=idle_chats_guard
        )
    except HTTPException as exc:
        raise _Refused("busy", f"{exc.detail}. Nothing was deleted.") from None


async def _delete_entry(target: Mapping[str, Any], _options: Mapping[str, Any]) -> bool:
    world, entry = await get_world(target["world_id"]), await get_lorebook_entry(target["entry_id"])
    if world is None or entry is None or entry["world_id"] != world["id"]:
        return False
    return await lorebook.delete_entry(world, entry)


async def _delete_chat(target: Mapping[str, Any], _options: Mapping[str, Any]) -> bool:
    cid = target["conversation_id"]
    if await get_conversation(cid) is None:
        return False
    # The chat list's own delete stops a running reply first; a change reviewed in a card waits for it instead.
    if conversation_busy(cid):
        raise _Refused("busy", _BUSY)
    try:
        return await delete_chat(cid)
    except HTTPException as exc:
        raise _Refused("busy", f"{exc.detail}.") from None


async def _delete_message(target: Mapping[str, Any], _options: Mapping[str, Any]) -> bool:
    cid, mid = target["conversation_id"], target["message_id"]
    message = await get_message_by_id(mid)
    if message is None or message["conversation_id"] != cid:
        return False
    if conversation_busy(cid):
        raise _Refused("busy", _BUSY)
    try:
        return await delete_message_subtree(cid, mid)
    except HTTPException as exc:
        raise _Refused("busy", f"{exc.detail}.") from None


async def _delete_document(target: Mapping[str, Any], _options: Mapping[str, Any]) -> bool:
    if await get_document(target["document_id"]) is None:
        return False
    try:
        return await delete_document_settled(target["document_id"])
    except HTTPException as exc:
        raise _Refused("busy", f"{exc.detail}.") from None


async def _delete_world(target: Mapping[str, Any], _options: Mapping[str, Any]) -> bool:
    return await delete_world(target["world_id"])


async def _delete_persona(target: Mapping[str, Any], _options: Mapping[str, Any]) -> bool:
    persona_id = target["persona_id"]
    if await get_user_persona(persona_id) is None or not await delete_user_persona(persona_id):
        return False
    # As the persona list's own delete does: the active persona must not name a persona that is gone.
    if (await get_settings()).get("active_persona_id") == persona_id:
        await update_settings({"active_persona_id": None})
    return True


async def _delete_phrase_group(target: Mapping[str, Any], _options: Mapping[str, Any]) -> bool:
    return await delete_phrase_group(target["group_id"])


_DELETERS: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], Awaitable[bool]]] = {
    "character": _delete_character,
    "lorebook_entry": _delete_entry,
    "conversation": _delete_chat,
    "message": _delete_message,
    "document": _delete_document,
    "world": _delete_world,
    "persona": _delete_persona,
    "phrase_group": _delete_phrase_group,
}


async def _sync_card(proposal: Proposal) -> None:
    """What the card route does after its write, in its own commit: safe to repeat, so a repeated Apply runs it again, and
    one that failed is retried until it succeeds."""
    if proposal.kind == "character" and proposal.action == "edit":
        card = await get_character_card(proposal.target["character_id"])
        if card is not None:
            await sync_conversations_for_card(proposal.target["character_id"], card)


async def _apply(proposal: Proposal) -> dict[str, Any]:
    """Write *proposal* once and keep the answer; a repeated Apply returns that answer without comparing again."""
    repeated = proposal.result is not None
    if proposal.result is None:
        if proposal.action == "delete":
            if not await _DELETERS[proposal.kind](proposal.target, proposal.options):
                raise _Refused("missing", _MISSING)
            summary = f"Deleted {proposal.label}."
            resources = [{**proposal.target, "deleted": True}]
            if proposal.kind == "persona":
                resources.append({"kind": "settings", "keys": ["active_persona_id"]})
        else:
            try:
                await _write(proposal, proposal.before, proposal.after)
            except StaleWrite:
                raise _Refused("stale", _STALE, fields=[proposal.name]) from None
            summary = f"Applied the edit to {change_label(proposal)}."
            resources = [dict(proposal.target)]
        proposal.result = {"verdict": "applied", "summary": summary, "resources": resources}
        proposal.expires = _clock() + PROPOSAL_TTL_SECS
    await _sync_card(proposal)
    return {**proposal.result, "repeated": True} if repeated else proposal.result


def _expired(proposal_id: str) -> dict[str, Any]:
    message = "This proposal has expired or is unknown to the server. Ask again."
    return {"proposal_id": proposal_id, "verdict": "expired", "message": message}


async def apply_proposals(proposal_ids: Sequence[str]) -> list[dict[str, Any]]:
    """One verdict per proposal, each compared and written on its own, under its own lock.

    Concurrent and repeated Applies of one proposal wait for the first and return its answer. Once admitted a write settles
    whole, through a dropped connection too: a cancelled await proves nothing about whether SQLite committed.
    """
    verdicts = []
    for proposal_id in dict.fromkeys(proposal_ids):
        proposal = get_proposal(proposal_id)
        if proposal is None:
            verdicts.append(_expired(proposal_id))
            continue
        async with proposal.lock:
            try:
                verdict = await settled(_apply(proposal))
            except _Refused as refused:
                verdict = refused.verdict
        verdicts.append({"proposal_id": proposal_id, **verdict})
    return verdicts


async def _undo(proposal: Proposal) -> dict[str, Any]:
    if proposal.action == "delete":
        raise _Refused("refused", "A delete cannot be undone.")
    if proposal.result is None:
        raise _Refused("refused", "That edit has not been applied, so there is nothing to undo.")
    try:
        await _write(proposal, proposal.after, proposal.before)
    except StaleWrite:
        raise _Refused("stale", "The text changed after the edit was applied, so it was left as it is.") from None
    # Pending again: the card offers Apply once more, and a further edit may build on it.
    proposal.result = None
    proposal.expires = _clock() + PROPOSAL_TTL_SECS
    await _sync_card(proposal)
    return {
        "verdict": "undone",
        "summary": f"Undid the edit to {change_label(proposal)}.",
        "resources": [dict(proposal.target)],
    }


async def undo_proposals(proposal_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Put back the stored text of each applied edit *proposal_ids* names, where it still holds what Apply wrote.

    ``undone`` returns the proposal to pending; ``stale``, the text changed since; ``missing``, ``busy`` and ``expired`` as for
    Apply; ``refused``, a delete or an edit not applied.
    """
    verdicts = []
    for proposal_id in dict.fromkeys(proposal_ids):
        proposal = get_proposal(proposal_id)
        if proposal is None:
            verdicts.append(_expired(proposal_id))
            continue
        async with proposal.lock:
            try:
                verdict = await settled(_undo(proposal))
            except _Refused as refused:
                verdict = refused.verdict
        verdicts.append({"proposal_id": proposal_id, **verdict})
    return verdicts


async def revise_proposal(proposal_id: str, text: str) -> dict[str, Any]:
    """Make *text*, the user's own, what the pending edit *proposal_id* writes; answers its new preview.

    Only a pending edit of a text field can be revised: a list is edited item by item, and a delete has no text.
    """
    proposal = get_proposal(proposal_id)
    if proposal is None:
        return _expired(proposal_id)
    async with proposal.lock:
        if proposal.result is not None:
            return {"proposal_id": proposal_id, "verdict": "refused", "message": "That edit is already applied."}
        if proposal.action != "edit" or not isinstance(proposal.after, str):
            return {"proposal_id": proposal_id, "verdict": "refused", "message": "Only a text edit can be revised."}
        # A message's macros resolve here, as when it was proposed, so Apply commits the text this preview shows.
        after = resolve_inline(text) if proposal.kind == "message" else text
        if (proposal.name == "name" or proposal.kind == "message") and not after.strip():
            return {"proposal_id": proposal_id, "verdict": "refused", "message": "That cannot be left empty."}
        if after == proposal.before:
            return {
                "proposal_id": proposal_id,
                "verdict": "refused",
                "message": "That is the stored text unchanged; Cancel the edit instead.",
            }
        revise(proposal, after)
        return {"proposal_id": proposal_id, "verdict": "revised", "proposal": preview(proposal)}
