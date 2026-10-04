"""Turn-owned events: fixed host payloads and validated, open hook JSON.

HookEvent is nominal so an open ``event: str`` cannot swallow the core union's
payload checks or discriminated narrowing. It remains a dict on the wire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, get_args, get_type_hints

from ..core.llm_types import ParsedToolCall
from ..database.models import WorldChangesetRow
from ..workflows.contracts import public_event_error

if TYPE_CHECKING:
    from .state import TurnResultData, TurnState


class HookEvent(dict[str, Any]):
    """A public envelope accepted at a turn-hook boundary, with open JSON data."""

    def __init__(self, envelope: object) -> None:
        if reason := turn_hook_event_error(envelope):
            raise ValueError(reason)
        assert isinstance(envelope, dict)
        super().__init__(envelope)


class FailureData(TypedDict):
    headline: str
    sentence: str
    kind: str
    stage: str
    status: NotRequired[int]
    host: NotRequired[str]
    model: NotRequired[str]
    body: NotRequired[str]
    workflow_id: NotRequired[str]


class PhaseData(TypedDict):
    channel: str
    label: NotRequired[str]
    state: NotRequired[str]


ReasoningData = TypedDict("ReasoningData", {"pass": str, "delta": str})


class DraftData(TypedDict):
    draft: str


class RewriteData(TypedDict):
    refined_text: str


class StepData(TypedDict):
    step: str


class WriterDoneData(TypedDict):
    editor_will_run: bool


class UserMessageData(TypedDict):
    id: int
    content: str


class DirectorData(TypedDict):
    active_moods: list[str]
    injection_block: str
    tool_calls: list[ParsedToolCall]
    agent_latency_ms: int
    extra_fields: dict[str, Any]
    fragment_cooldowns: dict[str, int]


class EditorData(TypedDict):
    tool_calls: list[ParsedToolCall]


class FeedbackData(TypedDict):
    values: dict[str, Any]


class StateData(TypedDict):
    changes: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    dropped: list[dict[str, Any]]


class DecisionsData(TypedDict):
    evaluations: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    cooldowns: dict[str, int]
    inherited: NotRequired[Literal[1]]


class WorldChangeData(TypedDict):
    message_id: int
    changeset: WorldChangesetRow


class AttachmentsRejectedData(TypedDict):
    message_id: int
    rejected: list[dict[str, Any]]


class SpeakerPlanItem(TypedDict):
    member_id: str
    card_id: str | None
    name: str
    cue: str


class SpeakingPlanData(TypedDict):
    exchange_id: str
    plan: list[SpeakerPlanItem]


class SpeakerStartData(TypedDict):
    exchange_id: str
    member_id: str
    card_id: str | None
    name: str
    index: int
    total: int
    cue: str


class SpeakerDoneData(TypedDict):
    exchange_id: str
    message_id: int | None
    parent_id: int | None
    turn_index: int
    member_id: str
    card_id: str | None
    name: str
    content: str


class DoneEvent(TypedDict):
    event: Literal["done"]


class DirectorStartEvent(TypedDict):
    event: Literal["director_start"]


class TokenEvent(TypedDict):
    event: Literal["token"]
    data: str


class ErrorEvent(TypedDict):
    event: Literal["error"]
    data: str


class FailureEvent(TypedDict):
    event: Literal["error"]
    data: FailureData


class WarningEvent(TypedDict):
    event: Literal["warning"]
    data: FailureData


class PhaseEvent(TypedDict):
    event: Literal["phase_status"]
    data: PhaseData


class ReasoningEvent(TypedDict):
    event: Literal["reasoning"]
    data: ReasoningData


class DraftEvent(TypedDict):
    event: Literal["draft_update"]
    data: DraftData


class RewriteEvent(TypedDict):
    event: Literal["writer_rewrite"]
    data: RewriteData


class StepEvent(TypedDict):
    event: Literal["step_start"]
    data: StepData


class WriterDoneEvent(TypedDict):
    event: Literal["writer_done"]
    data: WriterDoneData


class UserMessageEvent(TypedDict):
    event: Literal["user_message_created"]
    data: UserMessageData


class DirectorDoneEvent(TypedDict):
    event: Literal["director_done"]
    data: DirectorData


class EditorDoneEvent(TypedDict):
    event: Literal["editor_done"]
    data: EditorData


class FeedbackEvent(TypedDict):
    event: Literal["feedback"]
    data: FeedbackData


class StateEvent(TypedDict):
    event: Literal["state"]
    data: StateData


class DecisionsEvent(TypedDict):
    event: Literal["decisions"]
    data: DecisionsData


class WorldChangeEvent(TypedDict):
    event: Literal["world_change_proposed"]
    data: WorldChangeData


class AttachmentsRejectedEvent(TypedDict):
    event: Literal["workflow_attachments_rejected"]
    data: AttachmentsRejectedData


class SpeakingPlanEvent(TypedDict):
    event: Literal["speaking_plan"]
    data: SpeakingPlanData


class SpeakerStartEvent(TypedDict):
    event: Literal["speaker_start"]
    data: SpeakerStartData


class SpeakerDoneEvent(TypedDict):
    event: Literal["speaker_done"]
    data: SpeakerDoneData


class TurnStateEvent(TypedDict):
    event: Literal["_turn_state"]
    data: TurnState


class ResultEvent(TypedDict):
    event: Literal["_result"]
    data: TurnResultData


CoreTurnEvent = (
    DoneEvent
    | DirectorStartEvent
    | TokenEvent
    | ErrorEvent
    | FailureEvent
    | WarningEvent
    | PhaseEvent
    | ReasoningEvent
    | DraftEvent
    | RewriteEvent
    | StepEvent
    | WriterDoneEvent
    | UserMessageEvent
    | DirectorDoneEvent
    | EditorDoneEvent
    | FeedbackEvent
    | StateEvent
    | DecisionsEvent
    | WorldChangeEvent
    | AttachmentsRejectedEvent
    | SpeakingPlanEvent
    | SpeakerStartEvent
    | SpeakerDoneEvent
)
PublicTurnEvent = CoreTurnEvent | HookEvent
PipelineEvent = PublicTurnEvent | TurnStateEvent | ResultEvent

# Names come from the host event contract, traced to entrypoints, stages,
# persistence and chat_stream.js. Shared hooks may publish only the four below.
SHARED_HOOK_EVENTS = frozenset({"phase_status", "reasoning", "draft_update", "warning"})
PROTECTED_TURN_EVENTS = (
    frozenset(name for event_type in get_args(CoreTurnEvent) for name in get_args(get_type_hints(event_type)["event"]))
    - SHARED_HOOK_EVENTS
)


def turn_hook_event_error(ev: object) -> str | None:
    """Validate a turn hook's public ownership after generic envelope validation.

    On-demand/document/library streams have different owners; their generic
    encoders must not use this policy. JSON extension fields are retained.
    """
    reason = public_event_error(ev)
    if reason is not None:
        return reason
    if not isinstance(ev, dict):
        return "not a dict"
    name = ev["event"]
    if name in PROTECTED_TURN_EVENTS:
        return f"event {name!r} is protected by the turn host"
    if name not in SHARED_HOOK_EVENTS:
        return None
    data = ev.get("data")
    if not isinstance(data, dict):
        return f"{name} data must be a JSON object"
    required = {
        "phase_status": ("channel",),
        "reasoning": ("pass", "delta"),
        "draft_update": ("draft",),
        "warning": ("headline",),
    }[name]
    optional = {
        "phase_status": ("label", "state"),
        "warning": ("sentence", "kind", "stage", "host", "model", "body", "workflow_id"),
    }.get(name, ())
    for field in (*required, *(key for key in optional if key in data)):
        if not isinstance(data.get(field), str):
            return f"{name}.{field} must be a string"
    if name == "phase_status" and "label" not in data and "state" not in data:
        return "phase_status requires label or state"
    if name == "warning" and "status" in data and type(data["status"]) is not int:
        return "warning.status must be an integer"
    return None
