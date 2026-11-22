"""Pure turn placement: where a turn's rows land and what each group speaker receives.

The handlers in ``entrypoints.py`` load, call these, then persist and stream what they return. Nothing here awaits, touches the
database or mints an id: a fresh exchange id arrives as a parameter, so the same inputs always plan the same turn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core import GroupContextMode
from ..prompting import prefix_is_speaker_scoped, tail_carries_identity
from .state import SheetUpdateTurn

# ===============================================================================
# Shared placement rules
# ===============================================================================


def next_turn_index(messages: Sequence[Mapping[str, Any]]) -> int:
    """The turn index a row appended after *messages* takes."""
    return (messages[-1]["turn_index"] + 1) if messages else 0


def new_exchange_id(grouped: bool, fresh_id: str) -> str | None:
    """A new user row opens a new exchange; only a group conversation has exchanges."""
    return fresh_id if grouped else None


def sibling_exchange_ids(target: Mapping[str, Any], fresh_id: str) -> tuple[str, str | None]:
    """Return ``(exchange id to write, exchange id to replay decisions from)`` for a sibling of *target*.

    A regenerated reply is a sibling at the same turn index under the same parent, so it occupies the target's slot in the exchange
    it is replacing; *fresh_id* stands in only when the target predates exchange ids.
    """
    own = str(target.get("exchange_id") or "") or None
    return own or fresh_id, own


def latest_user_id(rows: Sequence[Mapping[str, Any]]) -> int | None:
    """The id of the last user row in *rows*, or None."""
    return next((m.get("id") for m in reversed(rows) if m.get("role") == "user"), None)


def history_attachments(attachments: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Re-key uploads into the shape history rows carry.

    The wire format (``mime``/``b64``) reaches a turn straight from the browser, while ``format_message_with_attachments`` reads
    history rows through the DB names. A later speaker in an exchange sees the user's image only as part of the replayed history
    -- not as its own trailing attachment -- so the two spellings have to meet here or the image silently stops at the first
    speaker.
    """
    out: list[dict] = []
    for att in attachments:
        b64 = att.get("data_b64") or att.get("b64") or ""
        if not b64:
            continue
        out.append({"mime_type": att.get("mime_type") or att.get("mime") or "image/jpeg", "data_b64": b64})
    return out


def db_attachments(attachments: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Normalize frontend attachment format to DB format before persisting."""
    return [
        {
            "mime_type": att.get("mime", att.get("mime_type", "image/jpeg")),
            "data_b64": att.get("b64", att.get("data_b64", "")),
            "filename": att.get("filename"),
            "size": att.get("size"),
        }
        for att in attachments
    ]


# ===============================================================================
# Send / continue
# ===============================================================================


@dataclass(frozen=True, slots=True)
class TurnPlacement:
    """Where a ``/send`` or ``/continue`` turn lands.

    *user_msg_id* is the existing user row a ``/continue`` answers, else None: a ``/send`` learns its id only once the shell has
    persisted the row (``persist_user``) at *user_turn* under *user_parent_id*.
    """

    history: Sequence[Mapping[str, Any]]
    user_msg_id: int | None
    user_turn: int
    user_parent_id: int | None
    persist_user: bool
    db_attachments: list[dict]
    exchange_id: str | None

    @property
    def asst_turn(self) -> int:
        return self.user_turn + 1


def place_turn(
    messages: Sequence[Mapping[str, Any]],
    *,
    active_leaf_id: int | None,
    continuing: bool,
    grouped: bool,
    attachments: Sequence[Mapping[str, Any]],
    fresh_exchange_id: str,
) -> TurnPlacement:
    """Place a ``/send`` (*continuing* False) or ``/continue`` turn after the active path *messages*."""
    history, user_msg_id = messages, None
    # For /continue the user row already exists; use its turn_index.
    user_turn = next_turn_index(messages)
    if continuing and messages and messages[-1]["role"] == "user":
        history, user_msg_id = messages[:-1], messages[-1]["id"]
        user_turn = messages[-1]["turn_index"]

    if not grouped:
        exchange_id = None
    elif continuing:
        # The pending row's own exchange. An empty path raises here, as it always has: there is no exchange to continue.
        exchange_id = str(messages[-1].get("exchange_id") or fresh_exchange_id)
    else:
        exchange_id = fresh_exchange_id
    return TurnPlacement(
        history=history,
        user_msg_id=user_msg_id,
        user_turn=user_turn,
        user_parent_id=active_leaf_id,
        persist_user=not continuing,
        db_attachments=[] if continuing else db_attachments(attachments),
        exchange_id=exchange_id,
    )


# ===============================================================================
# Regenerate / continue-reply
# ===============================================================================


def regen_history_parent_id(parent_msg: Mapping[str, Any] | None) -> int | None:
    """Where a regeneration's history stops: before the parent user row, through an assistant parent, nothing for a root."""
    if parent_msg is None:
        return None
    if parent_msg.get("role") == "user":
        return parent_msg.get("parent_id")
    return parent_msg.get("id")


def steered_history(
    history: Sequence[Mapping[str, Any]], parent_msg: Mapping[str, Any] | None, target: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    """*history* extended with the original exchange, for a steered regenerate.

    Include the original parent and target so the model sees what it wrote before being steered. In a group cascade the parent
    may itself be an assistant and may already be the end of ``history``.
    """
    extended = list(history)
    if parent_msg is not None and (not extended or extended[-1].get("id") != parent_msg.get("id")):
        extended.append(parent_msg)
    extended.append(target)
    return extended


def continued_history(path: Sequence[Mapping[str, Any]], kept: str) -> tuple[list[Mapping[str, Any]], str]:
    """The path ending in its reply cut to *kept*, and the last user message on it."""
    history = [*path[:-1], {**path[-1], "content": kept}]
    last_user = next((str(m.get("content") or "") for m in reversed(history) if m.get("role") == "user"), "")
    return history, last_user


def exchange_decision_input(
    history: Sequence[Mapping[str, Any]],
    user_message: str,
    parent_message_id: int | None,
    *,
    exchange_id: str | None,
    steering: str = "",
) -> tuple[Sequence[Mapping[str, Any]], str, int | None]:
    """Return the exchange's history, request, and anchor for decision replay.

    Regeneration rewinds to the exchange start. A turn without its own request
    reuses the pending user message, and steering is appended to the request.
    """
    before, request, anchor = history, user_message, parent_message_id
    if exchange_id is not None:
        first = next((index for index, row in enumerate(history) if str(row.get("exchange_id") or "") == exchange_id), None)
        if first is None:
            return history, user_message, parent_message_id
        row = history[first]
        before = history[:first]
        request = str(row.get("content") or "") if row.get("role") == "user" else ""
        anchor = row.get("id") if row.get("role") == "user" else (before[-1]["id"] if before else None)
    if not request and before and before[-1].get("role") == "user":
        pending = before[-1]
        before, request, anchor = before[:-1], str(pending.get("content") or ""), pending.get("id")
    return before, "\n\n".join(part for part in (request, steering) if part), anchor


# ===============================================================================
# Group exchange
# ===============================================================================

# The largest round any strategy can legitimately schedule (``group_max_speakers`` is capped at 8). A chip-click scene can chain
# requests without ever inserting a user row, so the lookback needs a ceiling that is not "the whole conversation".
ROUND_MAX_REPLIES = 8


def round_prefix(history: Sequence[Mapping[str, Any]], names: Mapping[str, str]) -> tuple[str, list[tuple[str, str]]]:
    """The round already on the branch: the user's last message and every reply since.

    An exchange is *request*-scoped. Under `manual` -- and for any cast-chip click on a resting scene -- one round is several
    requests, so this request's own replies are not the round. The sheet pass would otherwise be asked "did this exchange
    durably change Kael?" with the line that changed him in a different request, and on `handle_speak` with no user message at
    all. This is the same round ``workflows/image_gen/subjects.py`` reads, for the same reason, and it is consulted only when
    the request did not bring a user message of its own.
    """
    lines: list[tuple[str, str]] = []
    user_message = ""
    for row in reversed(history):
        if row.get("role") == "user":
            user_message = str(row.get("content") or "")
            break
        if row.get("role") != "assistant":
            continue
        content = str(row.get("content") or "")
        if content.strip():
            lines.append((names.get(str(row.get("speaker_member_id") or "")) or "Speaker", content))
        if len(lines) >= ROUND_MAX_REPLIES:
            break
    lines.reverse()
    return user_message, lines


@dataclass(frozen=True, slots=True)
class ExchangeFrame:
    """What every speaker of one group exchange shares, fixed once the speaking plan is known."""

    conversation_id: str
    exchange_id: str
    user_message: str
    attachments: Sequence[Mapping[str, Any]]
    history: Sequence[Mapping[str, Any]]
    first_turn_index: int
    total: int
    context_mode: GroupContextMode
    speaker_scoped: bool
    sheet_updates: bool
    prior_user: str
    prior_lines: Sequence[tuple[str, str]]


def exchange_frame(
    *,
    conversation_id: str,
    exchange_id: str,
    user_message: str,
    attachments: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    first_turn_index: int,
    total: int,
    context_mode: GroupContextMode,
    sheet_updates: bool,
    speaker_names: Mapping[str, str],
) -> ExchangeFrame:
    """Freeze an exchange's shared inputs; *total* is the length of the speaking plan."""
    # Only when this request brought no user message of its own: a `/send` starts a new round, so looking back would drag the
    # previous one into this one's evidence. Named through `ctx.speaker_names`, which covers members the roster has since
    # dropped -- their lines are still part of the round the sheet pass reads.
    prior_user, prior_lines = ("", []) if user_message else round_prefix(history, speaker_names)
    return ExchangeFrame(
        conversation_id=conversation_id,
        exchange_id=exchange_id,
        user_message=user_message,
        attachments=attachments,
        history=history,
        first_turn_index=first_turn_index,
        total=total,
        context_mode=context_mode,
        # Under Classic card swap the shared setup base is the *neutral* one -- the Director ran before a speaker was known -- so
        # even the first speaker has to rebuild its prefix around its own card. Reusing the setup base there is a correctness bug
        # in that mode, not merely a cache miss.
        speaker_scoped=prefix_is_speaker_scoped(context_mode),
        sheet_updates=sheet_updates,
        prior_user=prior_user,
        prior_lines=prior_lines,
    )


@dataclass(frozen=True, slots=True)
class ExchangeCursor:
    """Where the next speaker's reply attaches, and what the exchange has actually said so far.

    *spoke* is ``(member id, name, reply)`` in order. The sheet stage is gated on it rather than on the plan -- a planned speaker
    that never persisted a reply left no prose, so there is nothing about it to record and nothing to bill a call for.
    """

    parent_id: int | None
    spoke: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SpeakerInputs:
    """What one planned speaker's pipeline receives."""

    turn_index: int
    is_final: bool
    message: str
    attachments: Sequence[Mapping[str, Any]]
    history: Sequence[Mapping[str, Any]]
    reuse_setup_prefix: bool
    sheet_update: SheetUpdateTurn | None


def plan_speaker(
    frame: ExchangeFrame,
    index: int,
    *,
    grown_history: Sequence[Mapping[str, Any]],
    cursor: ExchangeCursor,
    member_id: str,
    name: str,
) -> SpeakerInputs:
    """Inputs for the planned speaker at *index*; *grown_history* is the exchange so far, as :func:`exchange_history` grows it."""
    # The exchange's last planned speaker carries every once-per-exchange step:
    # the Dynamic Worlds proposal, the sheet pass, and the after-reply state step.
    is_final = index == frame.total - 1
    if index == 0:
        message, attachments, history = frame.user_message, frame.attachments, frame.history
    else:
        message, attachments, history = "", [], grown_history
    # Once per exchange, on the last speaker: the pass reads the whole exchange, and running it per speaker would bill the same
    # members again for the same scene with one more line in it.
    sheet_update = (
        SheetUpdateTurn(
            conversation_id=frame.conversation_id,
            exchange_id=frame.exchange_id,
            # This request's speakers, not the round's: `spoke` is who to propose *about*, and an earlier request already billed a
            # call for the members it ran. Only the evidence below widens.
            member_ids=(*(mid for mid, _, _ in cursor.spoke), member_id),
            user_message=frame.user_message or frame.prior_user,
            speaker_name=name,
            lines=(*frame.prior_lines, *((spoke_name, text) for _, spoke_name, text in cursor.spoke)),
        )
        # The mode belongs in this condition and not only in Group settings. Under Shared and Swap a member's sheet is rendered into
        # the *cached* body, so an applied update rebuilds the whole scene prefix -- the exact cost the opt-in is gated on avoiding.
        # Leaving that invariant to one line of the client meant a `PUT` that changed only the mode left the pass running against
        # the layout it was never priced for.
        if is_final and frame.sheet_updates and tail_carries_identity(frame.context_mode)
        else None
    )
    return SpeakerInputs(
        turn_index=frame.first_turn_index + index,
        is_final=is_final,
        message=message,
        attachments=attachments,
        history=history,
        reuse_setup_prefix=index == 0 and not frame.speaker_scoped,
        sheet_update=sheet_update,
    )


def exchange_history(frame: ExchangeFrame, *, user_message_id: int | None, append_user: bool) -> list[Mapping[str, Any]]:
    """The history later speakers read: the exchange's history plus, when it is new here, the user's own row."""
    grown: list[Mapping[str, Any]] = list(frame.history)
    if append_user and user_message_id is not None and frame.user_message:
        grown.append(
            {
                "id": user_message_id,
                "role": "user",
                "content": frame.user_message,
                "turn_index": frame.first_turn_index - 1,
                "parent_id": frame.history[-1]["id"] if frame.history else None,
                "speaker_member_id": None,
                "exchange_id": frame.exchange_id,
                # Only the first speaker receives the uploads as its own trailing attachments; every later one has to read them off
                # this row, or an exchange would answer an image only one member ever saw.
                "user_attachments": history_attachments(frame.attachments),
            }
        )
    return grown


def advance_exchange(
    frame: ExchangeFrame,
    cursor: ExchangeCursor,
    turn: SpeakerInputs,
    *,
    message_id: int,
    content: str,
    member_id: str,
    name: str,
) -> tuple[ExchangeCursor, dict[str, Any]]:
    """Record a persisted reply: the cursor the next speaker starts from, and the history row the reply adds."""
    row = {
        "id": message_id,
        "role": "assistant",
        "content": content,
        "turn_index": turn.turn_index,
        "parent_id": cursor.parent_id,
        "speaker_member_id": member_id,
        "exchange_id": frame.exchange_id,
    }
    return ExchangeCursor(parent_id=message_id, spoke=(*cursor.spoke, (member_id, name, content))), row
