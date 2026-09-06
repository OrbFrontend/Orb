"""Define read-only workflow contexts, tool specs, and hook contracts."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any


def _readonly(obj: Any) -> Any:
    """Return a recursive read-only view of obj."""
    if isinstance(obj, dict):
        return MappingProxyType({k: _readonly(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(_readonly(v) for v in obj)
    if isinstance(obj, (set, frozenset)):
        return frozenset(_readonly(v) for v in obj)
    if isinstance(obj, bytearray):
        return bytes(obj)
    return obj


# Control-event discriminators a workflow hook may yield (the ``"type"`` key the
# bridge dispatches on). Defined once here, where the seam owns its contract, so
# the bridge and any workflow import the same names instead of duplicating bare
# string literals. The string values are the stable wire shape.
EV_ENABLE_TOOLS = "enable_tools"  # pre-pipeline
EV_SYSTEM_PROMPT = "system_prompt"  # pre-pipeline
EV_DRAFT_REPLACED = "draft_replaced"  # post-pipeline
EV_ATTACH_ARTIFACT = "attach_artifact"  # post-pipeline
EV_SET_MESSAGE_STATE = "set_message_state"  # post-pipeline


@dataclass
class ToolSpec:
    """A tool a workflow contributes to the global tool catalog.

    ``name`` must equal ``schema["function"]["name"]``. ``choice`` is the
    pre-built ``tool_choice`` payload (almost always
    ``{"type": "function", "function": {"name": name}}``) so forced-call
    sites can pass it directly to ``client.complete(tool_choice=...)``.
    ``standalone`` defaults to True: workflow tools stay out of the pipeline
    union and are only reachable via direct forced calls. Setting False
    merges the tool into ``enabled_schemas(...)``'s output (subject to the
    workflow's ``enable_tools`` yields gating it per turn).
    """

    name: str
    schema: dict
    choice: dict
    standalone: bool = True


@dataclass(frozen=True)
class PreCtx:
    """Inputs available to a workflow's pre-pipeline hook."""

    conversation_id: str
    history: tuple
    last_user_message: str
    settings: MappingProxyType
    prefix: tuple
    enabled_tools_pre_merge: MappingProxyType
    turn_scratch: dict
    client: Any
    kv_tracker: Any
    schema_overrides: MappingProxyType
    character_id: str | None = None
    character: MappingProxyType | None = None


@dataclass(frozen=True)
class PostCtx:
    """Inputs available to a workflow's post-pipeline hook."""

    conversation_id: str
    history: tuple
    draft: str
    effective_msg: str
    director_output: MappingProxyType
    settings: MappingProxyType
    prefix: tuple
    enabled_tools: MappingProxyType
    turn_scratch: dict
    client: Any
    kv_tracker: Any
    schema_overrides: MappingProxyType
    character_id: str | None = None
    character: MappingProxyType | None = None


@dataclass(frozen=True)
class OnDemandCtx:
    """Inputs available to a workflow's on-demand HTTP handler.

    No ``turn_scratch`` or ``kv_tracker``: on-demand handlers run outside
    any turn, Python locals serve in place of scratch, and on-demand LLM
    calls do not participate in turn cache accounting. ``client`` is the
    Writer lane; ``agent_client`` and ``agent_model_name`` are the resolved
    Agent lane, reusing that same client in single-model mode.
    """

    conversation_id: str
    history: tuple
    last_user_message: str
    settings: MappingProxyType
    client: Any
    agent_client: Any
    agent_model_name: str
    character_id: str | None = None
    character: MappingProxyType | None = None


@dataclass(frozen=True)
class RegenCtx:
    """Inputs available to a workflow's regenerate handler."""

    conversation_id: str
    message_id: int
    attachment_id: int
    original_attachment: MappingProxyType
    history: tuple
    last_user_message: str
    settings: MappingProxyType
    client: Any
    agent_client: Any
    agent_model_name: str
    character_id: str | None = None
    character: MappingProxyType | None = None


@dataclass(frozen=True)
class RerollGenCtx:
    """Inputs available to a workflow's reroll hook."""

    conversation_id: str
    message_id: int
    attachment_id: int
    original_attachment: MappingProxyType
    settings: MappingProxyType
    client: Any
    prior_consumption_metadata: MappingProxyType | None = None
    # Both routes pass this explicitly -- ``_build_reroll_gen_ctx`` makes it
    # required -- so the default covers only a ctx constructed directly, in a
    # test or out of tree. Reproducing is the safe end of it: a wrong ``True``
    # costs a render nobody asked for, while a wrong ``False`` hands
    # ``/rehydrate`` a *different* image and overwrites the row with it, which is
    # the one failure on these two routes that destroys something.
    replay: bool = True


@dataclass(frozen=True)
class QueryCtx:
    """Inputs available to a workflow's query hook."""

    settings: MappingProxyType


@dataclass(frozen=True)
class WorkflowEventStream:
    """Transport-neutral stream of public workflow events."""

    events: AsyncIterator[dict]


def public_event_error(ev: object) -> str | None:
    """Validate a public workflow event; return ``None`` if valid, else a short reason.

    A public event is a dict ``{"event": <name>, "data": <payload>}`` where
    ``name`` is a non-empty, single-line string that does not start with ``_``
    (the reserved prefix for internal control events) and ``payload`` is a
    string or a JSON-serializable (``allow_nan=False``) dict. ``data`` defaults
    to ``""`` when absent.

    One definition of the wire shape, shared by the pipeline bridge (pre/post
    hook pass-through events) and the API on-demand SSE encoder, so the two
    consumers cannot drift into subtly different notions of a valid event.
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

    PRE_PIPELINE and POST_PIPELINE fan out over every subscribed workflow
    per turn; ON_DEMAND, REGENERATE, REROLL_GEN, and QUERY are single-dispatch
    slots resolved by workflow id from an HTTP route. QUERY is the only one
    with no conversation in scope -- the global config/discovery surface.
    """

    PRE_PIPELINE = "pre_pipeline"
    POST_PIPELINE = "post_pipeline"
    ON_DEMAND = "on_demand"
    REGENERATE = "regenerate"
    REROLL_GEN = "reroll_gen"
    QUERY = "query"


PreHook = Callable[[PreCtx], AsyncIterator[dict]]
PostHook = Callable[[PostCtx], AsyncIterator[dict]]
# An on-demand hook returns either a plain JSON object (the API renders it as a
# JSON response) or a WorkflowEventStream (the API renders it as an SSE stream).
OnDemandResult = dict | WorkflowEventStream
OnDemandHook = Callable[[OnDemandCtx, dict], Awaitable[OnDemandResult]]
RegenHook = Callable[[RegenCtx, dict], Awaitable[list[dict]]]
RerollGenHook = Callable[[RerollGenCtx, dict, str], Awaitable["bytes | tuple[bytes, dict | None]"]]
QueryHook = Callable[[QueryCtx, dict], Awaitable[dict]]
