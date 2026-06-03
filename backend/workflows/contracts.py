"""Boundary contracts for the workflow subsystem.

Defines the five context dataclasses passed to workflow hooks, the
``ToolSpec`` declaration carried on a ``Workflow``, and the ``_readonly``
recursive wrapper that turns mutable orchestrator-derived structures into
deeply read-only views.

Mutation behavior: every Ctx is ``frozen=True`` (field reassignment raises
``FrozenInstanceError``) and mutable fields are expected to be passed in
already wrapped via ``_readonly(...)``. Any write into a wrapped container
at any nesting depth raises immediately: ``TypeError`` from
``MappingProxyType`` item assignment, ``AttributeError`` from ``tuple.append``
and ``frozenset.add``, ``TypeError`` from tuple item assignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, AsyncIterator, Awaitable, Callable


def _readonly(obj: Any) -> Any:
    """Return a recursive read-only view of *obj*.

    - dict -> MappingProxyType (keys/values recursed)
    - list / tuple -> tuple (values recursed)
    - set / frozenset -> frozenset (values recursed)
    - bytearray -> bytes
    - everything else returned as-is

    Strings, bytes, ints, floats, None, and other immutable primitives pass
    through unchanged. MappingProxyType passes through as-is (it isn't a
    ``dict`` subclass, so the dict branch does not match), which keeps the
    helper idempotent on already-wrapped values.
    """
    if isinstance(obj, dict):
        return MappingProxyType({k: _readonly(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(_readonly(v) for v in obj)
    if isinstance(obj, (set, frozenset)):
        return frozenset(_readonly(v) for v in obj)
    if isinstance(obj, bytearray):
        return bytes(obj)
    return obj


@dataclass
class ToolSpec:
    """A tool a workflow contributes to the global tool registry.

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
    """Inputs available to a workflow's pre-pipeline hook.

    Wrapped fields (``history``, ``settings``, ``prefix``,
    ``enabled_tools_pre_merge``) are recursively read-only at the point the
    orchestrator constructs this Ctx; mutation attempts at any nesting depth
    raise immediately. ``schema_overrides`` is a ``MappingProxyType`` frozen
    once at turn setup -- its top-level mapping cannot gain/lose/swap entries,
    but its schema *values* stay plain dicts so they remain json-serializable
    into the tools blob (only the values, never the container, are serialized).
    ``turn_scratch``, ``client``, and ``kv_tracker`` are intentionally not
    wrapped -- they are the documented mutation channel, the per-turn LLM
    client, and the per-turn cache aggregator respectively, ref-shared across
    every PreCtx and PostCtx in the same turn.

    ``prefix`` carries the pipeline prefix *before* extra system blocks
    contributed by pre-pipeline ``system_prompt`` yields have been
    appended; ``enabled_tools_pre_merge`` carries the pre-merge enable map
    (``settings["enabled_tools"]``, zeroed wholesale when ``agent_on`` is
    false). ``schema_overrides`` is the per-turn dynamic-schema map every
    pipeline pass passes into ``enabled_schemas(...)`` (today it carries
    the dynamic ``direct_scene`` built from this turn's director
    fragments). Pre-pipeline forced calls that want pipeline tools-bytes
    cache reuse pass ``prefix``, ``enabled_tools_pre_merge``, AND
    ``schema_overrides`` through to ``forced_tool_call``.
    """

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
    """Inputs available to a workflow's post-pipeline hook.

    Constructed fresh per workflow during post-pipeline iteration with the
    current draft (any prior hook's ``draft_replaced`` is already applied).
    ``history`` is the same read-only message list the turn's ``PreCtx``
    received: prior messages only, excluding this turn's user message and the
    assistant message being produced. The current user message is available
    as ``effective_msg``. ``prefix`` carries the final pipeline prefix --
    extras from pre-pipeline ``system_prompt`` yields have already been
    appended -- matching the bytes director / writer / editor saw.
    ``enabled_tools`` is the merged pipeline tool-enable map.
    ``schema_overrides`` is the per-turn dynamic-schema map the three
    pipeline passes used. Post-pipeline forced calls that want full KV cache
    reuse with the pipeline should pass ``prefix``, ``enabled_tools``, AND
    ``schema_overrides`` through to ``forced_tool_call``.

    The assistant message row does not exist yet at post-pipeline time, so
    its per-message workflow state cannot be written through the toolkit
    setter here. A hook commits it by yielding
    ``{"type": "set_message_state", "state": <dict>}``; the orchestrator
    writes the slot once the row is persisted.
    """

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
    calls do not participate in turn cache accounting.
    """

    conversation_id: str
    history: tuple
    last_user_message: str
    settings: MappingProxyType
    client: Any
    character_id: str | None = None
    character: MappingProxyType | None = None


@dataclass(frozen=True)
class RegenCtx:
    """Inputs available to a workflow's regenerate HTTP handler.

    This ctx feeds the *full-reprocess* path: the workflow consumes the
    sliced history (messages strictly before the anchor message) and may
    re-derive generation parameters from scratch. The two other regen
    flows -- same-params with a fresh seed, and same-params + same-seed
    rehydrate -- use the lighter ``RerollGenCtx`` instead.

    ``original_attachment`` carries the workflow_attachments row currently
    being regenerated; ``history`` is the conversation as sliced for this
    regenerate call. No ``turn_scratch`` or ``kv_tracker``: regen runs
    outside any turn.
    """

    conversation_id: str
    message_id: int
    attachment_id: int
    original_attachment: MappingProxyType
    history: tuple
    last_user_message: str
    settings: MappingProxyType
    client: Any
    character_id: str | None = None
    character: MappingProxyType | None = None


@dataclass(frozen=True)
class RerollGenCtx:
    """Inputs to a workflow's ``reroll_gen`` hook -- the shared backend for
    two routes (``/reroll-gen``, ``/rehydrate``) that ask the workflow to
    re-call its generation model with caller-supplied ``params`` + ``seed``
    and return bytes.

    The hook does not branch on the triggering route. It may return either
    raw ``bytes`` or a ``(bytes, dict | None)`` tuple; the optional dict is
    a fresh ``consumption_metadata`` payload honored on both routes -- it
    becomes the new sibling's metadata on reroll-gen, and overwrites the
    original row's metadata in place on rehydrate. A raw ``bytes`` return
    (or a ``None`` second element) leaves the stored value unchanged.

    ``prior_consumption_metadata`` is the parent attachment's stored
    ``consumption_metadata`` pre-decoded from JSON, exposed for workflows
    that want to reuse the parent's payload rather than compute a fresh
    one. ``history``, ``turn_scratch``, ``kv_tracker`` are absent by
    design: this ctx is for "no context work, just call the model".
    """

    conversation_id: str
    message_id: int
    attachment_id: int
    original_attachment: MappingProxyType
    settings: MappingProxyType
    client: Any
    prior_consumption_metadata: MappingProxyType | None = None


class HookType(Enum):
    """Identifies which pipeline slot a subscription binds to.

    PRE_PIPELINE and POST_PIPELINE fan out over every subscribed workflow
    per turn; ON_DEMAND, REGENERATE, and REROLL_GEN are single-dispatch
    slots resolved by workflow id from an HTTP route.
    """

    PRE_PIPELINE = "pre_pipeline"
    POST_PIPELINE = "post_pipeline"
    ON_DEMAND = "on_demand"
    REGENERATE = "regenerate"
    REROLL_GEN = "reroll_gen"


PreHook = Callable[[PreCtx], AsyncIterator[dict]]
PostHook = Callable[[PostCtx], AsyncIterator[dict]]
OnDemandHook = Callable[[OnDemandCtx, dict], Awaitable[dict]]
RegenHook = Callable[[RegenCtx, dict], Awaitable[list[dict]]]
RerollGenHook = Callable[[RerollGenCtx, dict, str], Awaitable["bytes | tuple[bytes, dict | None]"]]
