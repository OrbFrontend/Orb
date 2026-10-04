"""Define read-only workflow contexts, tool specs, and hook contracts."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, NotRequired, TypedDict

if TYPE_CHECKING:
    from ..inference import KVCacheTracker, LLMClient


def readonly_view(obj: Any) -> Any:
    """Return a recursive read-only view of obj."""
    if isinstance(obj, dict):
        return MappingProxyType({k: readonly_view(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(readonly_view(v) for v in obj)
    if isinstance(obj, (set, frozenset)):
        return frozenset(readonly_view(v) for v in obj)
    if isinstance(obj, bytearray):
        return bytes(obj)
    return obj


# Control-event discriminators a workflow hook may yield (the ``"type"`` key the bridge dispatches on). Defined once here, where
# the seam owns its contract, so the bridge and any workflow import the same names instead of duplicating bare string literals.
# The string values are the stable wire shape.
EV_ENABLE_TOOLS: Final = "enable_tools"  # pre-pipeline
EV_SYSTEM_PROMPT: Final = "system_prompt"  # pre-pipeline
EV_DRAFT_REPLACED: Final = "draft_replaced"  # post-pipeline
EV_ATTACH_ARTIFACT: Final = "attach_artifact"  # post-pipeline
EV_SET_MESSAGE_STATE: Final = "set_message_state"  # post-pipeline


class PublicEvent(TypedDict):
    """A public SSE event. Workflow-specific JSON data stays open."""

    event: str
    data: NotRequired[str | dict[str, Any]]


class EnableToolsEvent(TypedDict):
    type: Literal["enable_tools"]
    tools: set[str] | frozenset[str] | dict[str, Literal[True]]


class SystemPromptEvent(TypedDict):
    type: Literal["system_prompt"]
    block: str


class DraftReplacedEvent(TypedDict):
    type: Literal["draft_replaced"]
    draft: str


class AttachArtifactEvent(TypedDict):
    type: Literal["attach_artifact"]
    attachment: dict[str, Any]


class SetMessageStateEvent(TypedDict):
    type: Literal["set_message_state"]
    state: dict[str, Any]


PreEvent = PublicEvent | EnableToolsEvent | SystemPromptEvent
PostEvent = PublicEvent | DraftReplacedEvent | AttachArtifactEvent | SetMessageStateEvent


@dataclass
class ToolSpec:
    """Workflow-contributed tool; name must match schema.function.name.

    choice is a ready tool_choice payload. Standalone tools default to direct
    forced calls only; standalone=False joins enabled_schemas, gated per turn.
    """

    name: str
    schema: dict
    choice: dict
    standalone: bool = True


@dataclass(frozen=True)
class PreCtx:
    """Inputs available to a workflow's pre-pipeline hook."""

    conversation_id: str
    history: tuple[Mapping[str, Any], ...]
    last_user_message: str
    settings: Mapping[str, Any]
    prefix: tuple[Mapping[str, Any], ...]
    enabled_tools_pre_merge: Mapping[str, bool]
    turn_scratch: dict[str, Any]
    client: LLMClient
    kv_tracker: KVCacheTracker
    schema_overrides: Mapping[str, Mapping[str, Any]]
    character_id: str | None = None
    character: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PostCtx:
    """Inputs available to a workflow's post-pipeline hook.

    ``client``/``prefix`` are the Writer lane. ``agent_client`` and ``agent_model_name`` identify the resolved Agent execution
    target, which is the Writer target in single-model mode. The defaults keep a hand-built ``PostCtx`` (tests, out-of-tree
    callers) valid.
    """

    conversation_id: str
    history: tuple[Mapping[str, Any], ...]
    draft: str
    effective_msg: str
    director_output: Mapping[str, Any]
    settings: Mapping[str, Any]
    prefix: tuple[Mapping[str, Any], ...]
    enabled_tools: Mapping[str, bool]
    turn_scratch: dict[str, Any]
    client: LLMClient
    kv_tracker: KVCacheTracker
    schema_overrides: Mapping[str, Mapping[str, Any]]
    character_id: str | None = None
    character: Mapping[str, Any] | None = None
    agent_client: LLMClient | None = None
    agent_model_name: str = ""


@dataclass(frozen=True)
class OnDemandCtx:
    """Inputs available to a workflow's on-demand HTTP handler.

    No ``turn_scratch`` or ``kv_tracker``: on-demand handlers run outside any turn, Python locals serve in place of scratch, and
    on-demand LLM calls do not participate in turn cache accounting. ``client`` is the Writer lane; ``agent_client`` and
    ``agent_model_name`` are the resolved Agent lane, reusing that same client in single-model mode.
    """

    conversation_id: str
    history: tuple[Mapping[str, Any], ...]
    last_user_message: str
    settings: Mapping[str, Any]
    client: LLMClient
    agent_client: LLMClient
    agent_model_name: str
    character_id: str | None = None
    character: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RegenCtx:
    """Inputs available to a workflow's regenerate handler."""

    conversation_id: str
    message_id: int
    attachment_id: int
    original_attachment: Mapping[str, Any]
    history: tuple[Mapping[str, Any], ...]
    last_user_message: str
    settings: Mapping[str, Any]
    client: LLMClient
    agent_client: LLMClient
    agent_model_name: str
    character_id: str | None = None
    character: Mapping[str, Any] | None = None
    phase: Callable[[str], None] = lambda _label: None  # step label, streamed to a client that asks
    # Saves one attachment as a sibling now, instead of with the handler's return, and answers its id (None when the cache
    # rejected it). A handler that makes several renders keeps each as it lands, so Stop keeps them and the client sees them
    # arrive. None where no route is saving for it.
    keep: Callable[[dict], Awaitable[int | None]] | None = None
    # Sends one extra event on the regenerate stream, for a client that asks for events. The name must start with
    # "<workflow_id>_". Silent where no stream is attached.
    emit: Callable[[str, dict], None] = lambda _event, _data: None


@dataclass(frozen=True)
class RerollGenCtx:
    """Inputs available to a workflow's reroll hook."""

    conversation_id: str
    message_id: int
    attachment_id: int
    original_attachment: Mapping[str, Any]
    settings: Mapping[str, Any]
    client: LLMClient
    prior_consumption_metadata: Mapping[str, Any] | None = None
    # Both routes pass this explicitly -- ``_build_reroll_gen_ctx`` makes it required -- so the default covers only a ctx
    # constructed directly, in a test or out of tree. Reproducing is the safe end of it: a wrong ``True`` costs a render nobody
    # asked for, while a wrong ``False`` hands ``/rehydrate`` a *different* image and overwrites the row with it, which is the
    # one failure on these two routes that destroys something.
    replay: bool = True


@dataclass(frozen=True)
class QueryCtx:
    """Inputs available to a workflow's query hook."""

    settings: Mapping[str, Any]


@dataclass(frozen=True)
class UploadCtx:
    """Inputs available to a workflow's upload hook: one file for one character.

    No conversation, client, or lock: the hook takes the toolkit lock matching
    any state it rewrites, so slow processing of the file holds nothing.
    """

    settings: Mapping[str, Any]
    character_id: str
    character: Mapping[str, Any]
    filename: str
    data: bytes


@dataclass(frozen=True)
class ExportCtx:
    """Inputs available to a workflow's export hook.

    ``attachment`` is the row without its bytes, and ``stored_bytes`` reads them only when called (None once evicted): an export
    that can fetch the file from where it was made never loads the stored copy.
    """

    attachment_id: int
    attachment: Mapping[str, Any]
    consumption_metadata: Mapping[str, Any] | None
    stored_bytes: Callable[[], Awaitable[bytes | None]]


@dataclass(frozen=True)
class ExportedFile:
    """One attachment as its workflow exports it for download.

    ``note`` says, in one sentence the user sees, when the file is not the best
    version the workflow could have produced and why.
    """

    data: bytes
    mime: str
    filename: str
    note: str = ""


@dataclass(frozen=True)
class WorkflowEventStream:
    """Transport-neutral stream of public workflow events."""

    events: AsyncIterator[PublicEvent]


def public_event_error(ev: object) -> str | None:
    """Validate public {event, data}; return None or a rejection reason.

    Event names must be non-empty, single-line and not start with _. Data defaults to empty text and accepts strings or strict
    JSON-serializable dicts. Shared by pipeline hooks and on-demand SSE.
    """
    if not isinstance(ev, dict):
        return f"not a dict (type={type(ev).__name__})"
    name = ev.get("event")
    if not isinstance(name, str) or not name.strip() or "\r" in name or "\n" in name:
        return "event name must be a non-empty single-line string"
    if name.startswith("_"):
        return f"event name {name!r} uses the reserved internal prefix"
    data = ev.get("data", "")
    if not isinstance(data, (str, dict)):
        return f"data must be str or dict (type={type(data).__name__})"
    if isinstance(data, dict):
        try:
            json.dumps(data, allow_nan=False)
        except (TypeError, ValueError):
            return "data dict is not JSON-serializable"
    return None


class HookType(Enum):
    """Identifies which pipeline slot a subscription binds to.

    PRE_PIPELINE and POST_PIPELINE fan out over every subscribed workflow per turn; ON_DEMAND, REGENERATE, REROLL_GEN, QUERY,
    UPLOAD, and EXPORT are single-dispatch slots resolved by workflow id from an HTTP route. QUERY and UPLOAD have no
    conversation in scope: QUERY is the global config/discovery surface, and UPLOAD takes a file for one character. EXPORT is
    optional, even for artifact workflows.
    """

    PRE_PIPELINE = "pre_pipeline"
    POST_PIPELINE = "post_pipeline"
    ON_DEMAND = "on_demand"
    REGENERATE = "regenerate"
    REROLL_GEN = "reroll_gen"
    QUERY = "query"
    UPLOAD = "upload"
    EXPORT = "export"


PreHook = Callable[[PreCtx], AsyncIterator[PreEvent]]
PostHook = Callable[[PostCtx], AsyncIterator[PostEvent]]
# An on-demand hook returns either a plain JSON object (the API renders it as a
# JSON response) or a WorkflowEventStream (the API renders it as an SSE stream).
OnDemandResult = dict | WorkflowEventStream
OnDemandHook = Callable[[OnDemandCtx, dict], Awaitable[OnDemandResult]]
RegenHook = Callable[[RegenCtx, dict], Awaitable[list[dict]]]
RerollGenHook = Callable[[RerollGenCtx, dict, str], Awaitable["bytes | tuple[bytes, dict | None]"]]
QueryHook = Callable[[QueryCtx, dict], Awaitable[dict]]
# The second argument is the upload request's query-string parameters.
UploadHook = Callable[[UploadCtx, dict[str, str]], Awaitable[dict]]
# None means there is nothing left to export: the bytes are evicted and the workflow cannot fetch the file from anywhere else.
ExportHook = Callable[[ExportCtx], Awaitable[ExportedFile | None]]

WorkflowHook = PreHook | PostHook | OnDemandHook | RegenHook | RerollGenHook | QueryHook | UploadHook | ExportHook
