"""Persistent state fragments: configuration, the event fold, and the operation contract.

A state fragment remembers changing conversation state in one of two modes: one
current value, or multiple entries. Both modes share one storage shape -- a list
of active entries with stable ids -- and one event history of explicit entry
writes and retirements. The fold below knows nothing about modes, so replaying a
branch never depends on how a fragment is configured today; the mode only
decides which public operations are offered and how the value is rendered.

Everything here is pure. SQL lives in ``database/``; model-facing rendering in
``prompting/``; the model call in ``pipeline/``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

STATE_FIELD_TYPE = "state"

# One current value, or several independent entries.
STATE_MODES = ("value", "entries")
# After the completed reply (default), before the Writer, or never automatically.
STATE_UPDATES = ("after_reply", "before_writer", "manual")
# Which passes besides the updater see the current state.
STATE_INJECTS = ("off", "director", "writer", "both")
STATE_COLUMNS = ("state_mode", "state_update", "state_inject")

DEFAULT_STATE_MODE = "value"
DEFAULT_STATE_UPDATE = "after_reply"
DEFAULT_STATE_INJECT = "both"

# Fixed limits rather than per-fragment controls. The updater is told a list's
# count and this limit, and an add past it is rejected and reported, so a small
# model that never retires is noticed instead of silently stopping.
MAX_STATE_TEXT_CHARS = 800
MAX_ACTIVE_ENTRIES = 12

# Explicit entry operations, the only thing the history stores.
EVENT_OPS = ("add", "revise", "retire")
# Who made a change: the model, the user, or a Compress History snapshot.
EVENT_SOURCES = ("agent", "user", "carried")

# Public operations. The model gets ``set`` (one value) or ``add``/``retire``
# (entries); the State panel adds ``clear`` and in-place ``revise``.
PUBLIC_OPS = ("set", "clear", "add", "revise", "retire")
AGENT_OPS_BY_MODE: Mapping[str, frozenset[str]] = {
    "value": frozenset({"set"}),
    "entries": frozenset({"add", "retire"}),
}
USER_OPS_BY_MODE: Mapping[str, frozenset[str]] = {
    # revise/retire stay available so a user can merge several active entries
    # into one before the next update replaces them.
    "value": frozenset({"set", "clear", "revise", "retire"}),
    "entries": frozenset({"add", "revise", "retire"}),
}

# Legacy fragment types, mapped at the card read boundary and by the migration.
LEGACY_PROGRESSIVE = "progressive"
LEGACY_DIRECTION_NOTE = "direction_note"
LEGACY_TIMING_TO_UPDATE: Mapping[str, str] = {"pre_writer": "before_writer", "post_turn": "after_reply"}


def is_state_row(row: Mapping[str, Any]) -> bool:
    return row.get("field_type") == STATE_FIELD_TYPE


def _choice(value: Any, allowed: Sequence[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def legacy_state_settings(field_type: str, direction_note_timing: Any = None) -> dict[str, str] | None:
    """The explicit state settings a legacy fragment type converts to, or ``None``.

    ``progressive`` becomes one value updated before the Writer and injected into
    both passes: the Director already saw the prior value and the Writer saw
    ``old -> new``. ``direction_note`` becomes multiple entries on its own timing;
    card fragments never carried an inject setting, so they inject into both.
    """
    if field_type == LEGACY_PROGRESSIVE:
        return {"state_mode": "value", "state_update": "before_writer", "state_inject": "both"}
    if field_type == LEGACY_DIRECTION_NOTE:
        timing = direction_note_timing if isinstance(direction_note_timing, str) else "post_turn"
        return {
            "state_mode": "entries",
            "state_update": LEGACY_TIMING_TO_UPDATE.get(timing, "after_reply"),
            "state_inject": "both",
        }
    return None


def upgrade_legacy_fragment(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return *entry* with a legacy state type rewritten as an explicit state fragment.

    Shared card files keep the old types indefinitely, so the card read boundary
    and card export both route through here. Anything else passes through as a
    copy.
    """
    out = dict(entry)
    settings = legacy_state_settings(str(entry.get("field_type") or ""), entry.get("direction_note_timing"))
    if settings is None:
        return out
    out["field_type"] = STATE_FIELD_TYPE
    out.update(settings)
    out.pop("direction_note_timing", None)
    return out


@dataclass(frozen=True, slots=True)
class StateFragment:
    """One state fragment's captured configuration for a turn or a manual write."""

    id: str
    label: str
    # The model-facing heading (the injection label, falling back to the label).
    heading: str
    description: str
    mode: str = DEFAULT_STATE_MODE
    update: str = DEFAULT_STATE_UPDATE
    inject: str = DEFAULT_STATE_INJECT
    required: bool = False
    cooldown_turns: int = 0
    sort_order: int = 0
    enabled: bool = True

    @property
    def injects_director(self) -> bool:
        return self.inject in ("director", "both")

    @property
    def injects_writer(self) -> bool:
        return self.inject in ("writer", "both")

    @property
    def rides_direct_scene(self) -> bool:
        """A before-Writer one-value update is a ``direct_scene`` parameter."""
        return self.update == "before_writer" and self.mode == "value"

    @property
    def uses_state_tool(self) -> bool:
        """Every other automatic update goes through the ``update_state`` tool."""
        return self.update == "after_reply" or (self.update == "before_writer" and self.mode == "entries")


def state_fragment_of(row: Mapping[str, Any]) -> StateFragment | None:
    """Parse a state fragment row; unknown settings fall back to the defaults."""
    if not is_state_row(row):
        return None
    fid = str(row.get("id") or "")
    if not fid:
        return None
    label = str(row.get("label") or "").strip() or fid
    heading = str(row.get("injection_label") or "").strip() or label
    return StateFragment(
        id=fid,
        label=label,
        heading=heading,
        description=str(row.get("description") or ""),
        mode=_choice(row.get("state_mode"), STATE_MODES, DEFAULT_STATE_MODE),
        update=_choice(row.get("state_update"), STATE_UPDATES, DEFAULT_STATE_UPDATE),
        inject=_choice(row.get("state_inject"), STATE_INJECTS, DEFAULT_STATE_INJECT),
        required=bool(row.get("required")),
        cooldown_turns=int(row.get("cooldown_turns") or 0),
        sort_order=int(row.get("sort_order") or 0),
        enabled=bool(row.get("enabled", 1)),
    )


def state_fragments_of(rows: Iterable[Mapping[str, Any]]) -> tuple[StateFragment, ...]:
    """Every state fragment in *rows*, in the given order."""
    return tuple(fragment for row in rows if (fragment := state_fragment_of(row)) is not None)


# ── The fold ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StateEntry:
    """One active entry: its stable id, text, and the change that last wrote it."""

    entry_id: str
    text: str
    message_id: int | None = None
    source: str = "agent"


@dataclass(slots=True)
class StateView:
    """Active entries per fragment, in insertion order, plus each fragment's saved label.

    Built by folding events in active-path order. Mutable so the operation
    contract below can apply a turn's changes to a working copy; ``copy()``
    before mutating a view that is shared.
    """

    entries: dict[str, dict[str, StateEntry]] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    def copy(self) -> StateView:
        return StateView({fid: dict(active) for fid, active in self.entries.items()}, dict(self.labels))

    def active(self, fragment_id: str) -> list[StateEntry]:
        return list(self.entries.get(fragment_id, {}).values())

    def has_entry(self, fragment_id: str, entry_id: str) -> bool:
        return entry_id in self.entries.get(fragment_id, {})

    def fragment_ids(self) -> list[str]:
        """Fragments with saved history, in first-write order."""
        return list(dict.fromkeys([*self.labels, *self.entries]))

    def apply(self, event: Mapping[str, Any]) -> None:
        """Apply one explicit event. Unknown ops and missing entries are no-ops."""
        fid = str(event.get("fragment_id") or "")
        entry_id = str(event.get("entry_id") or "")
        if not fid or not entry_id:
            return
        label = event.get("fragment_label")
        if isinstance(label, str) and label:
            self.labels[fid] = label
        op = event.get("op")
        active = self.entries.setdefault(fid, {})
        message_id = event.get("message_id")
        source = str(event.get("source") or "agent")
        if op == "add":
            active[entry_id] = StateEntry(entry_id, str(event.get("text") or ""), message_id, source)
        elif op == "revise":
            if entry_id in active:
                active[entry_id] = StateEntry(entry_id, str(event.get("text") or ""), message_id, source)
        elif op == "retire":
            active.pop(entry_id, None)
        if not active:
            self.entries.pop(fid, None)

    def as_dict(self) -> dict[str, list[dict[str, Any]]]:
        """JSON-safe projection, keyed by fragment id."""
        return {
            fid: [
                {"entry_id": e.entry_id, "text": e.text, "message_id": e.message_id, "source": e.source}
                for e in active.values()
            ]
            for fid, active in self.entries.items()
        }


def fold_events(events: Iterable[Mapping[str, Any]], base: StateView | None = None) -> StateView:
    """Fold events, already in active-path order, into the active entries.

    The fold is mode-agnostic by design: a value written as one value reads the
    same after the fragment switches to multiple entries, and vice versa.
    """
    view = base.copy() if base is not None else StateView()
    for event in events:
        view.apply(event)
    return view


def value_text(entries: Sequence[StateEntry]) -> str:
    """Render active entries as one value; several entries render as a list."""
    if not entries:
        return ""
    if len(entries) == 1:
        return entries[0].text
    return "\n" + "\n".join(f"- {entry.text}" for entry in entries)


# ── The operation contract ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StateOp:
    """A public operation on one fragment, before validation."""

    op: str
    fragment_id: str
    text: str = ""
    entry_id: str = ""
    # The model-facing alias an entry was listed under, for reports.
    alias: str = ""


@dataclass(frozen=True, slots=True)
class StateRejection:
    """An operation that was not applied, with a machine reason and a sentence."""

    fragment_id: str
    op: str
    reason: str
    detail: str
    text: str = ""
    entry: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "fragment_id": self.fragment_id,
            "op": self.op,
            "reason": self.reason,
            "detail": self.detail,
            "text": self.text,
            "entry": self.entry,
        }


def new_entry_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def plan_state_ops(
    ops: Iterable[StateOp],
    fragments: Mapping[str, StateFragment],
    view: StateView,
    *,
    source: str,
    new_id: Callable[[], str] = new_entry_id,
) -> tuple[list[dict[str, Any]], list[StateRejection]]:
    """Validate public operations and translate them into explicit entry events.

    Operations apply in order to *view* (mutated in place), so a retire frees a
    slot for a later add in the same call. Returns the events -- without an
    anchor, which the caller stamps when it commits -- and the rejections.

    Omission is ``keep``: empty text never clears or retires anything, and a
    rejected operation leaves the state as it was. ``fragments`` is the captured
    contract; an id outside it is unknown even if it has saved history.
    """
    allowed_by_mode = AGENT_OPS_BY_MODE if source == "agent" else USER_OPS_BY_MODE
    events: list[dict[str, Any]] = []
    rejections: list[StateRejection] = []

    def reject(op: StateOp, reason: str, detail: str) -> None:
        rejections.append(StateRejection(op.fragment_id, op.op, reason, detail, op.text, op.alias or op.entry_id))

    def emit(fragment: StateFragment, op: str, entry_id: str, text: str | None = None) -> None:
        if op == "retire" and text is None:
            # Keep the retired text, so history and the Inspector can say what went.
            retired = view.entries.get(fragment.id, {}).get(entry_id)
            text = retired.text if retired else None
        event: dict[str, Any] = {
            "fragment_id": fragment.id,
            "entry_id": entry_id,
            "op": op,
            "text": text,
            "mode": fragment.mode,
            "fragment_label": fragment.label,
            "source": source,
        }
        view.apply(event)
        events.append(event)

    for op in ops:
        fragment = fragments.get(op.fragment_id)
        if fragment is None:
            reject(op, "unknown_fragment", f"No state fragment named {op.fragment_id!r}.")
            continue
        if op.op not in allowed_by_mode[fragment.mode]:
            reject(op, "wrong_mode", f"{op.op!r} does not apply to {fragment.label} in its current mode.")
            continue
        text = op.text.strip()
        active = view.active(fragment.id)

        if op.op in ("set", "add", "revise"):
            if not text:
                if op.op == "revise":
                    reject(op, "empty", "A revision needs text; retire the entry to remove it.")
                continue  # Empty means keep.
            if len(text) > MAX_STATE_TEXT_CHARS:
                reject(op, "too_long", f"Longer than {MAX_STATE_TEXT_CHARS} characters.")
                continue

        if op.op == "set":
            if len(active) == 1:
                if active[0].text != text:
                    emit(fragment, "revise", active[0].entry_id, text)
                continue
            # Several active entries (left by a mode switch) are replaced by one;
            # the originals stay in history as retired entries.
            for entry in active:
                emit(fragment, "retire", entry.entry_id)
            emit(fragment, "add", new_id(), text)
        elif op.op == "clear":
            for entry in active:
                emit(fragment, "retire", entry.entry_id)
        elif op.op == "add":
            if any(_normalized(entry.text) == _normalized(text) for entry in active):
                reject(op, "duplicate", "Already an active entry.")
                continue
            if len(active) >= MAX_ACTIVE_ENTRIES:
                reject(op, "full", f"{fragment.label} already holds {MAX_ACTIVE_ENTRIES} entries; retire some first.")
                continue
            emit(fragment, "add", new_id(), text)
        elif op.op in ("revise", "retire"):
            if not view.has_entry(fragment.id, op.entry_id):
                reject(op, "unknown_entry", f"No active entry {op.alias or op.entry_id!r} in {fragment.label}.")
                continue
            if op.op == "revise":
                current = next(entry for entry in active if entry.entry_id == op.entry_id)
                if current.text != text:
                    emit(fragment, "revise", op.entry_id, text)
            else:
                emit(fragment, "retire", op.entry_id)
    return events, rejections


def carry_events(events: Iterable[Mapping[str, Any]], view: StateView) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-apply a regenerated reply's user-made events onto its parent path.

    An ``add`` always applies. A ``revise`` or ``retire`` whose entry does not
    exist on the parent path -- the discarded reply added it -- has nothing to
    apply to and is dropped. Returns ``(applied, dropped)``; *view* is mutated.
    """
    applied: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for event in events:
        copy = {key: event.get(key) for key in ("fragment_id", "entry_id", "op", "text", "mode", "fragment_label", "source")}
        if copy["op"] != "add" and not view.has_entry(str(copy["fragment_id"]), str(copy["entry_id"])):
            dropped.append(copy)
            continue
        view.apply(copy)
        applied.append(copy)
    return applied, dropped


def full_fragments(view: StateView, fragments: Iterable[StateFragment]) -> set[str]:
    """Entries-mode fragments at the active-entry limit."""
    return {
        fragment.id
        for fragment in fragments
        if fragment.mode == "entries" and len(view.active(fragment.id)) >= MAX_ACTIVE_ENTRIES
    }
