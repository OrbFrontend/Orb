"""The State panel's reads and the user's manual state operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ... import database as db
from ...core import (
    MAX_ACTIVE_ENTRIES,
    MAX_STATE_TEXT_CHARS,
    STATE_FIELD_TYPE,
    StateEntry,
    StateFragment,
    StateOp,
    fold_events,
    plan_state_ops,
    state_fragment_of,
)


class StateWriteError(ValueError):
    """A manual operation that cannot be applied; ``status`` is the HTTP status to report."""

    def __init__(self, message: str, *, status: int = 400, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ConfiguredFragment:
    """A configured state fragment and its source."""

    fragment: StateFragment
    origin: str  # "global" | "card"


async def configured_state_fragments(conv: Mapping[str, Any]) -> list[ConfiguredFragment]:
    """Return global and card state fragments, with global ids taking precedence."""
    globals_ = [row for row in await db.get_interactive_fragments() if row.get("field_type") == STATE_FIELD_TYPE]
    card = await db.get_character_card(conv["character_card_id"]) if conv.get("character_card_id") else None
    _, card_rows, _ = await db.cast_embedded_fragments(card, await db.resolve_cast(conv))
    out: list[ConfiguredFragment] = []
    seen: set[str] = set()
    for rows, origin in ((globals_, "global"), (card_rows, "card")):
        for row in rows:
            fragment = state_fragment_of(row)
            if fragment is None or fragment.id in seen:
                continue
            seen.add(fragment.id)
            out.append(ConfiguredFragment(fragment, origin))
    return out


def _entry_projection(entry: StateEntry, turns: Mapping[int, int]) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "text": entry.text,
        "source": entry.source,
        "message_id": entry.message_id,
        "turn_index": turns.get(entry.message_id) if entry.message_id is not None else None,
    }


async def state_panel(cid: str) -> dict[str, Any]:
    """Return configured state and saved state for deleted fragments."""
    conv = await db.get_conversation(cid)
    if conv is None:
        raise StateWriteError("Conversation not found", status=404)
    path = await db.get_active_path(cid)
    turns = {m["id"]: m["turn_index"] for m in path}
    events = await db.get_state_events_for_path(cid, list(turns))
    view = fold_events(events)
    configured = await configured_state_fragments(conv)
    configured_ids = {item.fragment.id for item in configured}

    fragments: list[dict[str, Any]] = []
    for item in configured:
        fragment = item.fragment
        entries = view.active(fragment.id)
        fragments.append(
            {
                "fragment_id": fragment.id,
                "label": fragment.label,
                "mode": fragment.mode,
                "update": fragment.update,
                "inject": fragment.inject,
                "origin": item.origin,
                "enabled": fragment.enabled,
                "configured": True,
                "read_only": not fragment.enabled,
                "full": fragment.mode == "entries" and len(entries) >= MAX_ACTIVE_ENTRIES,
                "several_values": fragment.mode == "value" and len(entries) > 1,
                "entries": [_entry_projection(entry, turns) for entry in entries],
            }
        )
    with_history = list(dict.fromkeys(str(event["fragment_id"]) for event in events))
    for fid in with_history:
        if fid in configured_ids:
            continue
        entries = view.active(fid)
        fragments.append(
            {
                "fragment_id": fid,
                "label": view.labels.get(fid) or fid,
                "mode": None,
                "update": None,
                "inject": None,
                "origin": None,
                "enabled": False,
                "configured": False,
                "read_only": True,
                "full": False,
                "several_values": False,
                "entries": [_entry_projection(entry, turns) for entry in entries],
            }
        )
    settings = await db.get_settings()
    return {
        "fragments": fragments,
        "has_state": bool(events),
        "updates_on": bool(settings.get("state_updates", 1)),
        "limits": {"text": MAX_STATE_TEXT_CHARS, "entries": MAX_ACTIVE_ENTRIES},
    }


async def state_history(cid: str, fragment_id: str) -> list[dict[str, Any]]:
    """One fragment's changes on the active branch, newest first."""
    path = await db.get_active_path(cid)
    turns = {m["id"]: m["turn_index"] for m in path}
    events = [e for e in await db.get_state_events_for_path(cid, list(turns)) if e["fragment_id"] == fragment_id]
    return [
        {
            "op": event["op"],
            "entry_id": event["entry_id"],
            "text": event["text"],
            "mode": event["mode"],
            "source": event["source"],
            "message_id": event["message_id"],
            "turn_index": turns.get(event["message_id"]),
            "created_at": event["created_at"],
        }
        for event in reversed(events)
    ]


async def apply_manual_op(cid: str, op: StateOp) -> list[dict[str, Any]]:
    """Validate and commit one user operation on the active branch's leaf."""
    conv = await db.get_conversation(cid)
    if conv is None:
        raise StateWriteError("Conversation not found", status=404)
    leaf_id = conv.get("active_leaf_id")
    if not leaf_id:
        raise StateWriteError("There is no message on this branch to anchor the change to.", status=409)
    configured = {item.fragment.id: item.fragment for item in await configured_state_fragments(conv)}
    fragment = configured.get(op.fragment_id)
    if fragment is None:
        raise StateWriteError("That fragment no longer exists; its saved state is read-only.", status=409, reason="read_only")
    if not fragment.enabled:
        raise StateWriteError("That fragment is disabled; its saved state is read-only.", status=409, reason="read_only")
    if op.op in ("set", "add", "revise") and not op.text.strip():
        raise StateWriteError("The text is empty.", reason="empty")

    path = await db.get_active_path(cid)
    view = await db.fold_path_state(cid, [m["id"] for m in path])
    events, rejections = plan_state_ops([op], {fragment.id: fragment}, view, source="user")
    if rejections:
        rejection = rejections[0]
        raise StateWriteError(rejection.detail or rejection.reason, status=422, reason=rejection.reason)
    await db.add_state_events(cid, int(leaf_id), events)
    return events


async def delete_orphaned_state(cid: str, fragment_id: str) -> int:
    """Delete the saved state of a fragment that no longer exists, on every branch."""
    conv = await db.get_conversation(cid)
    if conv is None:
        raise StateWriteError("Conversation not found", status=404)
    if any(item.fragment.id == fragment_id for item in await configured_state_fragments(conv)):
        raise StateWriteError("That fragment still exists; clear or retire its state instead.", status=409, reason="configured")
    return await db.delete_fragment_state(cid, fragment_id)
