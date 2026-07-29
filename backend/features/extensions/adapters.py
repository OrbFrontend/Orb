"""Generic host adapters: compiled flows as workflow subscriptions and actions.

This is the seam between Orb's trusted hook contracts and the interpreter. One
adapter per binding, each a closure over an *already compiled* flow -- there is
no per-package code, no generated callable, and nothing here that reads a
package file lazily. Replacing or garbage-collecting a content directory
therefore cannot change an invocation already in flight.

The invocation transaction lives here, in the order the design fixes:

1. Derive the entity/state scopes the compiled flow may touch and acquire their
   locks in the canonical host order (workflow config, conversation state,
   character state).
2. Resolve a capability-filtered context and read state under those locks.
3. Execute steps with quotas and cancellation, staging every Orb-owned effect.
4. Only on a successful return, commit the staged set under the same locks.
5. Publish fixed host events after the commit.

Holding the locks from the state read through the commit is what preserves
atomic read-modify-write even when a model call sits in the middle. It
serializes only the same extension/entity keys, matching the trusted workflow
behavior -- and because the community adapter takes them itself, the pipeline
bridge must *not* wrap it in the same non-reentrant locks.

Effects a hook produces do not become SSE event names of the package's
choosing. They ride two fixed public events, ``extension_status`` and
``extension_effects``, whose payloads carry the closed effect vocabulary from
:mod:`.contracts.effects`. The frontend owns what each resource refetches.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

from ...analysis import ContributedFinding
from ...core import (
    WriterToolResult,
    conversation_stream_lock,
    workflow_character_state_lock,
    workflow_config_lock,
    workflow_state_lock,
)
from ...database import (
    commit_extension_state,
    get_conversation,
    get_db,
    get_message_by_id,
    get_workflow_character_state,
    get_workflow_config,
    get_workflow_state,
    switch_to_branch,
    update_character_card,
)
from ...inference import agent_lane_from_settings
from ...workflows.attachment_cache import insert_workflow_attachments
from ...workflows.contracts import (
    EV_ATTACH_ARTIFACT,
    EV_CONTEXT_BLOCK,
    EV_DRAFT_REPLACED,
    EV_SET_MESSAGE_STATE,
    AuditDetectorBinding,
    AuditDetectorRequest,
    AuditDetectorSpec,
    HookStage,
    HookType,
    PostCtx,
    PreCtx,
    RegenCtx,
    RerollGenCtx,
    WriterToolBinding,
    WriterToolRequest,
)
from ...workflows.fragment_types import (
    FragmentReduceRequest,
    FragmentTypeDefinition,
    FragmentTypeError,
    FragmentTypeInstance,
)
from . import artifacts, execution, secrets, telemetry
from .compiler import CompiledPackage
from .contracts import (
    AuditDetectorDescriptor,
    Capability,
    EffectEnvelope,
    Flow,
    OpContext,
    WriterToolDescriptor,
    iter_steps,
    parse_schema,
)
from .ctx import build_ctx
from .digest import canonical_json_bytes
from .errors import FlowCancelled, FlowError
from .interpreter import (
    DELETED,
    FlowResult,
    HostServices,
    Invocation,
    ModelLane,
    StagedEffects,
    run_flow,
    unimplemented_operations,
)
from .limits import (
    MAX_AUDIT_FINDING_NOTE_CHARS,
    MAX_AUDIT_FINDING_SNIPPET_CHARS,
    MAX_AUDIT_FINDINGS_PER_DETECTOR,
    MAX_STATE_BYTES_PER_SCOPE,
    MAX_WRITER_TOOL_ARGUMENT_BYTES,
    MAX_WRITER_TOOL_RESULT_BYTES,
)
from .network import HttpService, granted_origins
from .values import assert_json_bounds, render_template
from .writer_tools import writer_tool_input_schema, writer_tool_output_schema

logger = logging.getLogger(__name__)

EV_EXTENSION_STATUS = "extension_status"
EV_EXTENSION_EFFECTS = "extension_effects"
"""The two public event names a community invocation can produce.

Fixed and host-owned. A package contributes the *payload* of an effect from a
closed resource vocabulary; it never contributes a name, so no package string
becomes an event name, a DOM selector, a callback, or a module path.
"""

LOCKED_SCOPES: tuple[str, ...] = ("config", "conversation", "character")
"""State scopes with a host lock, in canonical acquisition order.

``message`` is absent deliberately: a post-hook write targets the assistant row
that does not exist yet, so it is staged into the pipeline result and committed
with that row rather than locked here.
"""


def fragment_type_bindings(compiled: CompiledPackage) -> tuple[FragmentTypeDefinition, ...]:
    """Compile package fragment descriptors into workflow-owned bindings.

    The returned definitions contain only immutable metadata and host callables
    closed over this exact compiled revision.  The pipeline consumes those
    callables through ``workflows/`` and never imports the extension feature.
    """
    bindings: list[FragmentTypeDefinition] = []
    for declared in compiled.manifest.contributions.fragment_types:
        config_schema = parse_schema(
            declared.config_schema,
            what=f"fragment_type {declared.id!r} config_schema",
        )
        director_template = parse_schema(
            declared.director_schema,
            allow_config_templates=True,
            what=f"fragment_type {declared.id!r} director_schema",
        )
        flow = compiled.flows[declared.reduce_flow]
        definition_ref: list[FragmentTypeDefinition] = []

        def instantiate(
            raw_config: Mapping[str, Any],
            *,
            _declared=declared,
            _config_schema=config_schema,
            _director_template=director_template,
            _flow=flow,
            _definition_ref=definition_ref,
        ) -> FragmentTypeInstance:
            config = dict(raw_config)
            reason = _config_schema.validate(config)
            if reason is not None:
                raise FragmentTypeError(f"type configuration is invalid: {reason}")
            resolved_schema = _resolve_fragment_schema(_director_template.schema, config)
            concrete_schema = parse_schema(
                resolved_schema,
                what=f"fragment_type {_declared.id!r} resolved director_schema",
            )

            async def reduce_value(request: FragmentReduceRequest) -> Any:
                return await _run_fragment_reducer(compiled, _flow, request)

            def prior(fragment: Mapping[str, Any], previous: Any) -> str:
                template = _declared.prior_context
                if template is None or previous is None:
                    return ""
                return render_template(
                    template["$template"],
                    {"fragment": _fragment_template_namespace(fragment, config, previous=previous)},
                )

            def writer(fragment: Mapping[str, Any], previous: Any, current: Any) -> str:
                template = _declared.writer_context
                if template is None:
                    return f"{fragment['injection_label']}: {canonical_json_bytes(current).decode('utf-8')}"
                return render_template(
                    template["$template"],
                    {
                        "fragment": _fragment_template_namespace(
                            fragment,
                            config,
                            previous=previous,
                            current=current,
                        )
                    },
                )

            # Frozen v1 seed convention: a validated top-level ``initial``
            # config member becomes the normalized ``{"value": initial}``
            # prior used by Meter-shaped progressive descriptors. Descriptors
            # without that member simply have no seed and must wait for a
            # Director/reducer result or a persisted branch value.
            has_initial = "initial" in config
            initial = {"value": config["initial"]} if has_initial else None
            return FragmentTypeInstance(
                definition=_definition_ref[0],
                config=config,
                director_schema=concrete_schema.schema,
                validate_director=concrete_schema.validate,
                reduce_value=reduce_value,
                render_prior=prior,
                render_writer=writer,
                has_initial=has_initial,
                initial_value=initial,
            )

        config_view = compiled.views.get(declared.config_view) if declared.config_view else None
        value_view = compiled.views.get(declared.value_view) if declared.value_view else None
        definition = FragmentTypeDefinition(
            type_id=f"{compiled.extension_id}:{declared.id}",
            local_id=declared.id,
            label=declared.label,
            description=declared.description or "",
            storage=declared.storage,
            config_schema=config_schema.schema,
            instantiate=instantiate,
            prompt_hint=f"structured {declared.label.lower()} value",
            owner_id=compiled.extension_id,
            content_digest=compiled.digest,
            config_view=config_view.model_dump(mode="json") if config_view is not None else None,
            value_view=value_view.model_dump(mode="json") if value_view is not None else None,
        )
        definition_ref.append(definition)
        bindings.append(definition)
    return tuple(bindings)


def _resolve_fragment_schema(value: Any, config: Mapping[str, Any]) -> Any:
    """Fill the descriptor's two numeric schema-template forms."""
    if isinstance(value, dict):
        if set(value) == {"$config"}:
            return config[value["$config"]]
        if set(value) == {"$neg_config"}:
            return -config[value["$neg_config"]]
        return {key: _resolve_fragment_schema(item, config) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_fragment_schema(item, config) for item in value]
    return value


def _fragment_template_namespace(
    fragment: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    previous: Any = None,
    current: Any = None,
    director: Any = None,
) -> dict[str, Any]:
    namespace = {
        "id": fragment.get("id"),
        "label": fragment.get("label"),
        "description": fragment.get("description"),
        "injection_label": fragment.get("injection_label"),
        "config": dict(config),
        "previous": previous,
    }
    if current is not None:
        namespace["current"] = current
    if director is not None:
        namespace["director"] = director
    return namespace


async def _run_fragment_reducer(
    compiled: CompiledPackage,
    flow: Flow,
    request: FragmentReduceRequest,
) -> Any:
    """Run one pure reducer with lifecycle coordination and live consent."""
    from .runtime import live_grants

    granted = live_grants(compiled.extension_id)
    if (Capability.FRAGMENT_TYPE_CONTRIBUTE.value, None) not in granted:
        raise FragmentTypeError("fragment type contribution permission is not granted")

    async def no_state(_scope: str) -> Mapping[str, Any]:
        raise FlowError("fragment reducers cannot read extension state")

    invocation = Invocation(
        extension_id=compiled.extension_id,
        context=OpContext.REDUCER,
        host=HostServices(
            grants=lambda: live_grants(compiled.extension_id),
            read_state=no_state,
            is_cancelled=request.is_cancelled,
            charge_step=request.budget.charge_step,
        ),
        fragment=_fragment_template_namespace(
            request.fragment,
            request.config,
            previous=request.previous,
            director=request.director,
        ),
    )
    result: FlowResult | None = None
    try:
        async with execution.track_invocation(compiled.extension_id):
            async for event in run_flow(flow, invocation):
                if isinstance(event, FlowResult):
                    result = event
            if result is None:
                raise FragmentTypeError("fragment reducer did not finish")
            _validate_staged_effects(invocation, result)
    except (execution.InvocationBlocked, FlowError) as exc:
        raise FragmentTypeError(str(exc)) from None
    assert result is not None
    request.budget.charge(value=result.value)
    return result.value


# ── Writer tools ─────────────────────────────────────────────────────────────


def writer_tool_binding(compiled: CompiledPackage, blocked: Sequence[str]) -> WriterToolBinding | None:
    """Bind this revision's Writer tool, or publish nothing at all.

    Nothing at all is the load-bearing case, exactly as with the artifact pair:
    a blocked contribution must keep its *schema* out of the snapshot, not just
    its executor. A published schema with no runnable flow would put a tool in
    front of the model that Orb would then have to refuse -- spending prompt
    bytes and a turn to say no.
    """
    declared = compiled.manifest.writer_tool
    spec = compiled.writer_tool
    if declared is None or spec is None or "writer tool" in blocked:
        return None
    flow = compiled.flows.get(declared.flow)
    if flow is None:
        return None
    return WriterToolBinding(spec=spec, invoke=make_writer_tool(compiled, declared, flow))


def make_writer_tool(
    compiled: CompiledPackage,
    declared: WriterToolDescriptor,
    flow: Flow,
) -> Callable[[WriterToolRequest], Awaitable[WriterToolResult]]:
    """Bind a compiled flow as this extension's Writer-tool executor.

    The executor runs inside an *unfinished* model turn, which is what shapes
    everything unusual about it:

    * the arguments are validated against the compiled input schema before any
      step runs, so a hostile or malformed call costs nothing;
    * ``ctx.draft`` is whatever prose the Writer has already streamed, supplied
      by the host -- the model cannot describe a different draft, and the
      package cannot ask it to;
    * there is no message row, so ``message`` scope is unreachable (the
      compiler already refused it) and no artifact may be attached;
    * a successful invocation is its own state transaction. It commits when the
      *flow* returns, not when the *turn* succeeds, so a namespaced write can
      outlive a Writer abort. That is documented in the consent copy rather
      than papered over: a model call and an HTTP request already could not be
      rolled back, and pretending the state write can be would be the lie.
    """
    extension_id = compiled.extension_id
    input_schema = writer_tool_input_schema(declared)
    output_schema = writer_tool_output_schema(declared)
    key = compiled.writer_tool.key if compiled.writer_tool is not None else None

    async def invoke(request: WriterToolRequest) -> WriterToolResult:
        call = request.invocation
        granted = _granted(extension_id)
        if (Capability.WRITER_TOOL_CONTRIBUTE.value, None) not in granted:
            raise FlowError("permission writer.tool.contribute is not granted")

        arguments = dict(call.arguments)
        assert_json_bounds(arguments, what="the Writer tool's arguments")
        argument_bytes = _assert_writer_argument_bytes(arguments)
        reason = input_schema.validate(arguments)
        if reason is not None:
            raise FlowError(f"the Writer tool's arguments do not match its declared schema: {reason}")

        access = _StateAccess(
            extension_id,
            conversation_id=call.conversation_id,
            character_id=request.character_id,
        )
        invocation = Invocation(
            extension_id=extension_id,
            context=OpContext.WRITER_TOOL,
            host=_services(
                compiled,
                access,
                lanes=_turn_lanes(request),
                is_cancelled=request.is_cancelled,
            ),
            ctx=build_ctx(
                extension_id=extension_id,
                hook="writer_tool",
                granted=granted,
                conversation_id=call.conversation_id,
                card=request.card,
                history=request.history,
                last_user_message=request.last_user_message,
                draft=call.draft,
                direction=request.direction,
            ),
            action_input=arguments,
            metadata={
                "conversation_id": call.conversation_id,
                "hook": "writer_tool",
                "writer_tool": key.local_id if key is not None else declared.id,
                "writer_tool_input_bytes": argument_bytes,
            },
            # Conversation, host-owned turn-attempt identity and invocation
            # ordinal, revision, tool, and provider call id. The ordinal is what
            # distinguishes calls within an attempt: compatible providers may
            # reuse their correlation id across separate completions.
            seed=(
                f"writer_tool|{call.conversation_id or ''}|{call.turn_seed}|{call.invocation_index}|"
                f"{compiled.digest}|{declared.id}|{call.call_id}"
            ),
            scopes_in_scope=flow_scopes(flow),
            output_schema=output_schema,
            shared_budget=request.turn_budget,
        )

        result: FlowResult | None = None
        async for event in _invoke(
            compiled=compiled,
            flow=flow,
            invocation=invocation,
            access=access,
            entry_point=f"writer tool {declared.id}",
        ):
            if isinstance(event, FlowResult):
                result = event
        assert result is not None
        return WriterToolResult(value=result.value)

    return invoke


def _assert_writer_argument_bytes(arguments: Mapping[str, Any]) -> int:
    size = len(canonical_json_bytes(dict(arguments)))
    if size > MAX_WRITER_TOOL_ARGUMENT_BYTES:
        raise FlowError(f"the Writer tool's arguments are {size} bytes, over the {MAX_WRITER_TOOL_ARGUMENT_BYTES} byte limit")
    return size


def _assert_encoded_writer_result(value: Any) -> int:
    """Bound the result *before* it becomes part of the model's transcript.

    Tighter than an action's return budget on purpose: this value is spliced
    into an unfinished turn, where every byte is context the Writer pays for
    and cannot decline.
    """
    size = len(canonical_json_bytes(value))
    if size > MAX_WRITER_TOOL_RESULT_BYTES:
        raise FlowError(f"the Writer tool's result is {size} bytes, over the {MAX_WRITER_TOOL_RESULT_BYTES} byte limit")
    return size


# ── Audit detectors ──────────────────────────────────────────────────────────

_FINDING_SCHEMA = parse_schema(
    {
        "type": "array",
        "maxItems": MAX_AUDIT_FINDINGS_PER_DETECTOR,
        "items": {
            "type": "object",
            "properties": {
                "snippet": {"type": "string", "maxLength": MAX_AUDIT_FINDING_SNIPPET_CHARS},
                "note": {"type": "string", "maxLength": MAX_AUDIT_FINDING_NOTE_CHARS},
            },
            "required": ["snippet", "note"],
        },
    },
    what="audit detector result",
)
"""What a detector flow must return, fixed by the host.

The one structural difference from the Writer tool: a package declares no output
schema here, so it cannot widen what a finding *is*. ``snippet`` may be empty --
that is the whole-draft finding, which renders in the report and produces no
prefill target -- but it must be present, so "no span" is a stated choice rather
than a missing key.
"""


def audit_detector_bindings(compiled: CompiledPackage, blocked: Sequence[str]) -> tuple[AuditDetectorBinding, ...]:
    """Bind this revision's audit detectors, or publish none at all.

    All or nothing, like the fragment-type catalog and for the same reason: the
    contributions publish under one consent grant, so a set where some detectors
    run and others silently do not is a worse outcome than a package the manager
    can explain.
    """
    declared = compiled.manifest.contributions.audit_detectors
    if not declared or "audit detectors" in blocked:
        return ()
    bindings: list[AuditDetectorBinding] = []
    for descriptor in declared:
        flow = compiled.flows.get(descriptor.flow)
        if flow is None:
            return ()
        spec = AuditDetectorSpec(
            key=f"{compiled.extension_id}:{descriptor.id}",
            label=descriptor.label,
            content_digest=compiled.digest,
        )
        bindings.append(AuditDetectorBinding(spec=spec, invoke=make_audit_detector(compiled, descriptor, flow, spec)))
    return tuple(bindings)


def make_audit_detector(
    compiled: CompiledPackage,
    declared: AuditDetectorDescriptor,
    flow: Flow,
    spec: AuditDetectorSpec,
) -> Callable[[AuditDetectorRequest], Awaitable[tuple[ContributedFinding, ...]]]:
    """Bind a compiled flow as one of this extension's audit detectors.

    The Editor's mirror of :func:`make_writer_tool`, with the same trust profile
    and one different failure rule:

    * it runs inside an *unfinished* turn, so there is no message row (the
      compiler already refused ``message`` scope), no artifact to attach, no UI
      surface listening for a toast, and no user click to justify a first-party
      mutation;
    * ``ctx.draft`` is the post-Writer text the host supplies -- a detector
      scores the draft Orb has, not one it describes;
    * **any failure yields zero findings and never fails the turn.** A refused
      grant, a timeout, a malformed return, a blocked invocation: all of them
      mean "this check did not run", which is the same philosophy as
      ``RESOLVER_UNAVAILABLE``. The reply is not the place to surface a
      package's problem.
    """
    extension_id = compiled.extension_id

    async def invoke(request: AuditDetectorRequest) -> tuple[ContributedFinding, ...]:
        granted = _granted(extension_id)
        if (Capability.AUDIT_DETECTOR_CONTRIBUTE.value, None) not in granted:
            raise FlowError("permission audit.detector.contribute is not granted")

        access = _StateAccess(
            extension_id,
            conversation_id=request.conversation_id,
            character_id=request.character_id,
        )
        invocation = Invocation(
            extension_id=extension_id,
            context=OpContext.DETECTOR,
            host=_services(
                compiled,
                access,
                lanes=_turn_lanes(request),
                is_cancelled=request.is_cancelled,
            ),
            ctx=build_ctx(
                extension_id=extension_id,
                hook="audit_detector",
                granted=granted,
                conversation_id=request.conversation_id,
                card=request.card,
                history=request.history,
                last_user_message=request.last_user_message,
                draft=request.draft,
                direction=request.direction,
            ),
            metadata={
                "conversation_id": request.conversation_id,
                "hook": "audit_detector",
                "audit_detector": declared.id,
            },
            # Conversation, host-owned turn-attempt identity, revision, and
            # detector. Same construction as the Writer tool with ``detector|``
            # as the discriminator, so regeneration is fresh even when the draft
            # and history are identical, and two detectors in one turn do not
            # share a random stream.
            seed=f"detector|{request.conversation_id or ''}|{request.turn_seed}|{compiled.digest}|{declared.id}",
            scopes_in_scope=flow_scopes(flow),
            output_schema=_FINDING_SCHEMA,
        )

        result: FlowResult | None = None
        async for event in _invoke(
            compiled=compiled,
            flow=flow,
            invocation=invocation,
            access=access,
            entry_point=f"audit detector {declared.id}",
        ):
            if isinstance(event, FlowResult):
                result = event
        assert result is not None
        return tuple(
            ContributedFinding(
                detector_id=spec.key,
                # Stamped from the binding, never from the returned item: a
                # per-finding heading would be package text rendered as though
                # Orb had classified it.
                label=spec.label,
                snippet=str(item.get("snippet", "")),
                note=str(item.get("note", "")),
            )
            for item in result.value
            if isinstance(item, Mapping)
        )

    return invoke


def flow_scopes(flow: Flow) -> frozenset[str]:
    """The state scopes *flow* reads or writes, for its lock plan.

    Derived statically from the compiled flow, so the lock set is known before
    any step runs and cannot grow mid-invocation -- which is what makes a fixed
    acquisition order sufficient to prevent deadlock.
    """
    return frozenset(step.scope for step in iter_steps(flow.steps) if step.op.startswith("state."))


def flow_activates_branch(flow: Flow) -> bool:
    """Whether *flow* needs the conversation stream lock.

    Only branch activation does. A library-scoped action deliberately takes no
    stream lock: the invocation has no conversation to serialize against, and
    acquiring one per card would put a 300-deep queue in front of any live
    turn. The two are easy to conflate as "actions that write outside their own
    namespace", which is why the distinction is a named predicate rather than
    an inline check.
    """
    return any(step.op == "conversation.branch.activate" for step in iter_steps(flow.steps))


# ── state access ─────────────────────────────────────────────────────────────


class _StateAccess:
    """Reads and writes one extension's slot in each locked scope.

    One object rather than free functions so the read used to seed the
    invocation and the write used to commit it resolve the same entity ids.
    A commit that recomputed which conversation it belonged to would be a
    commit that could land on a different one.
    """

    def __init__(
        self,
        extension_id: str,
        *,
        conversation_id: str | None,
        character_id: str | None,
        pending_message: bool = False,
    ):
        self.extension_id = extension_id
        self.conversation_id = conversation_id
        self.character_id = character_id
        self.pending_message = pending_message

    def available(self, scopes: frozenset[str]) -> frozenset[str]:
        """The requested scopes that actually have an entity in this invocation."""
        present = {"config"}
        if self.conversation_id:
            present.add("conversation")
        if self.character_id:
            present.add("character")
        if self.pending_message:
            present.add("message")
        return frozenset(scopes & present)

    async def read(self, scope: str) -> Mapping[str, Any]:
        if scope == "config":
            return await get_workflow_config(self.extension_id) or {}
        if scope == "conversation" and self.conversation_id:
            return await get_workflow_state(self.conversation_id, self.extension_id) or {}
        if scope == "character" and self.character_id:
            return await get_workflow_character_state(self.character_id, self.extension_id) or {}
        if scope == "message" and self.pending_message:
            return {}
        raise FlowError(f"there is no {scope} in scope for this invocation")


async def _locks(stack: AsyncExitStack, access: _StateAccess, scopes: frozenset[str]) -> None:
    """Acquire the invocation's locks in the canonical order.

    Only the scopes the compiled flow actually touches, so two extensions that
    share no state never serialize against each other, and one that reads only
    its config never blocks a turn on a conversation lock.
    """
    for scope in LOCKED_SCOPES:
        if scope not in scopes:
            continue
        if scope == "config":
            await stack.enter_async_context(workflow_config_lock())
        elif scope == "conversation" and access.conversation_id:
            await stack.enter_async_context(workflow_state_lock(access.conversation_id, access.extension_id))
        elif scope == "character" and access.character_id:
            await stack.enter_async_context(workflow_character_state_lock(access.character_id, access.extension_id))


def _state_updates(
    access: _StateAccess,
    effects: StagedEffects,
    current: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Fold staged state writes into their post-invocation values."""
    updates: dict[str, dict[str, Any]] = {}
    for scope, staged in effects.state.items():
        if scope == "message":
            continue
        merged = dict(current.get(scope) or {})
        for path, value in staged.items():
            if value is DELETED:
                merged.pop(path, None)
            else:
                merged[path] = value
        updates[scope] = merged
    return updates


# ── shared invocation driver ─────────────────────────────────────────────────


def _effects_list(extension_id: str, effects: StagedEffects, conversation_id: str | None) -> list[dict[str, Any]]:
    """The closed-vocabulary effect list one committed invocation produced.

    One effect per thing the invocation *did*, not per repaint the frontend
    should perform. A renderer-driven sweep therefore emits one
    ``character.card`` per card and the frontend debounces; asking the host to
    emit fewer would make the envelope describe the UI's intentions rather than
    the invocation's writes.
    """
    listed: list[dict[str, Any]] = []
    if effects.card_tags is not None:
        listed.append({"resource": "character.card", "card_id": effects.card_tags[0]})
    if effects.branch_activation is not None and conversation_id:
        # The same three the built-in switch-branch button invalidates, so the
        # extension path repaints identically rather than approximately.
        listed.extend(
            [
                {
                    "resource": "conversation.messages",
                    "conversation_id": conversation_id,
                },
                {
                    "resource": "conversation.director",
                    "conversation_id": conversation_id,
                },
                {
                    "resource": "conversation.direction_notes",
                    "conversation_id": conversation_id,
                },
            ]
        )
    if effects.artifacts and conversation_id:
        # An action-attached artifact changes what the message list should be
        # showing. The same effect the built-in attachment routes cause a
        # refetch through, so the extension path repaints identically.
        listed.append({"resource": "conversation.messages", "conversation_id": conversation_id})
    listed.extend({"resource": "extension.view", "extension_id": extension_id, "view": view} for view in effects.invalidations)
    # One invocation that both activates a branch and attaches a file names the
    # message list twice. Deduplicated here rather than by the frontend: an
    # effect is a statement about a resource, and stating it twice is noise the
    # envelope's own budget would eventually pay for.
    return list({canonical_json_bytes(effect): effect for effect in listed}.values())


def _effect_payload(
    extension_id: str,
    effects: StagedEffects,
    generation: int,
    *,
    data: Any = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Translate staged effects into the fixed host envelope."""
    envelope = EffectEnvelope.model_validate(
        {
            "data": data,
            "effects": _effects_list(extension_id, effects, conversation_id),
            "toasts": effects.toasts,
            "runtime_generation": generation,
        }
    )
    return envelope.model_dump(mode="json")


def _validate_staged_effects(invocation: Invocation, result: FlowResult) -> None:
    """Revalidate cancellation, authority, budgets, and the fixed envelope."""
    if invocation.host.is_cancelled():
        raise FlowCancelled("the invocation was cancelled")
    effects = result.effects
    for scope in effects.state:
        invocation.require(Capability.STATE_WRITE, scope)
    for block in effects.context_blocks:
        for target in block["targets"]:
            invocation.require(Capability.PROMPT_CONTEXT_APPEND, target)
    if effects.draft is not None:
        invocation.require(Capability.DRAFT_REPLACE)
    if effects.card_tags is not None:
        invocation.require(Capability.CARD_WRITE, "tags")
        invocation.require(Capability.CONTEXT_READ, "character")
    if effects.branch_activation is not None:
        invocation.require(Capability.CONVERSATION_BRANCH_ACTIVATE)
    if effects.artifacts:
        invocation.require(Capability.ARTIFACT_WRITE)
    if effects.invalidations:
        invocation.require(Capability.UI_CONTRIBUTE)
    if invocation.host.context_block_error is not None:
        reason = invocation.host.context_block_error(effects.context_blocks)
        if reason is not None:
            raise FlowError(reason)
    # Validate the exact public shape before any state reaches the database.
    _effect_payload(
        invocation.extension_id,
        effects,
        0,
        data=result.value,
        conversation_id=str(invocation.metadata.get("conversation_id") or "") or None,
    )


async def _commit_card_tags(db, effects: StagedEffects) -> None:
    """Write the staged tag list through the ordinary card update path.

    Through ``update_character_card`` rather than straight into the column,
    because that is where the shared normalizer runs -- the same one the
    character API goes through. Writing the column directly would be the second
    tag-writing path, and the whole reason ``card.tags.set`` is admissible is
    that there is only one.

    The supplied connection owns the invocation transaction, so the normalized
    tag write lands with every other staged database effect or not at all.
    """
    if effects.card_tags is None:
        return
    card_id, tags = effects.card_tags
    if await update_character_card(card_id, {"tags": tags}, db=db) is None:
        raise FlowError("the character card this action targets no longer exists")


def artifact_payloads(compiled: CompiledPackage, effects: StagedEffects, seed: str) -> list[dict[str, Any]]:
    """Every staged artifact as the framework's attachment dict."""
    return [
        artifacts.attachment_payload(
            staged,
            extension_id=compiled.extension_id,
            version=compiled.manifest.version,
            content_digest=compiled.digest,
            seed=seed,
        )
        for staged in effects.artifacts
    ]


async def _commit_artifacts(
    db,
    compiled: CompiledPackage,
    effects: StagedEffects,
    seed: str,
) -> None:
    """Attach the artifacts that named their own target message.

    Only those: a post hook's artifact rides the pipeline result and commits
    with the assistant row it belongs to, and a recovery flow's is handed back
    to the framework route that asked for it. The discriminator is the target
    the compiler already forced each context to declare, so this does not have
    to know which kind of invocation it is in.
    """
    by_message: dict[int, list[dict[str, Any]]] = {}
    for staged in effects.artifacts:
        if staged.message_id is None:
            continue
        payload = artifacts.attachment_payload(
            staged,
            extension_id=compiled.extension_id,
            version=compiled.manifest.version,
            content_digest=compiled.digest,
            seed=seed,
        )
        by_message.setdefault(staged.message_id, []).append(payload)
    for message_id, payloads in by_message.items():
        _attachment_ids, rejected = await insert_workflow_attachments(message_id, payloads, db=db)
        if rejected:
            reason = rejected[0].get("reason") or "the attachment was refused"
            raise FlowError(f"the artifact could not be attached: {reason}")


async def _commit_branch(db, effects: StagedEffects, conversation_id: str | None) -> None:
    """Activate the staged branch through the same helper /switch-branch uses.

    Called while the conversation stream lock is still held, so it serializes
    against a running turn exactly as the built-in button does. The ownership
    re-check inside ``switch_to_branch`` is redundant under that lock and kept
    anyway: it is the storage-level guarantee, and a lock is a process-level
    one.
    """
    if effects.branch_activation is None:
        return
    if not conversation_id or not await switch_to_branch(conversation_id, effects.branch_activation, db=db):
        raise FlowError("the selected message is not part of this conversation")


async def _commit_staged(
    compiled: CompiledPackage | None,
    invocation: Invocation,
    access: _StateAccess,
    result: FlowResult,
) -> None:
    """Commit every database-backed staged effect in one transaction."""
    effects = result.effects
    updates = _state_updates(access, effects, invocation.state_snapshots)
    has_database_effect = bool(
        effects.card_tags is not None
        or any(staged.message_id is not None for staged in effects.artifacts)
        or updates
        or effects.branch_activation is not None
    )
    if not has_database_effect:
        _validate_staged_effects(invocation, result)
        return
    if any(staged.message_id is not None for staged in effects.artifacts) and compiled is None:
        raise FlowError("the invocation has no revision binding for its artifact")

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # The live-grant/cancellation check belongs inside the write
            # transaction. No permission mutation can commit between it and
            # the staged writes.
            _validate_staged_effects(invocation, result)
            await _commit_card_tags(db, effects)
            if compiled is not None:
                await _commit_artifacts(db, compiled, effects, invocation.seed)
            if updates:
                await commit_extension_state(
                    access.extension_id,
                    updates,
                    conversation_id=access.conversation_id,
                    character_id=access.character_id,
                    db=db,
                )
            await _commit_branch(db, effects, access.conversation_id)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def _invoke(
    *,
    compiled: CompiledPackage | None = None,
    flow: Flow,
    invocation: Invocation,
    access: _StateAccess,
    entry_point: str = "flow",
) -> AsyncIterator[dict | FlowResult]:
    """Run one flow under its locks and commit iff it returns successfully.

    Yields the interpreter's progress dicts and one terminal
    :class:`~.interpreter.FlowResult`. Any exception leaves every staged effect
    uncommitted: the failure path is "do nothing", not "undo what was done",
    which is why a flow that stages a state write and then gets an invalid model
    response leaves the prior state untouched.

    The stream lock, when the flow activates a branch, is acquired *outside*
    the state locks. That is the pipeline's own order (stream, then workflow
    locks); taking them the other way round is the deadlock the flow parser
    already refuses to let a model call sit inside.
    """
    conversation_id = access.conversation_id
    timer = telemetry.InvocationTimer(invocation.extension_id, entry_point)
    outcome: telemetry.Outcome = "error"
    writer_tool_output_bytes: int | None = None
    try:
        async with execution.track_invocation(invocation.extension_id):
            scopes = access.available(invocation.scopes_in_scope)
            invocation.scopes_in_scope = scopes
            async with AsyncExitStack() as stack:
                if flow_activates_branch(flow) and conversation_id:
                    await stack.enter_async_context(conversation_stream_lock(conversation_id))
                await _locks(stack, access, scopes)
                result: FlowResult | None = None
                async for event in run_flow(flow, invocation):
                    if isinstance(event, FlowResult):
                        result = event
                    else:
                        yield event
                assert result is not None
                if invocation.context == OpContext.WRITER_TOOL:
                    # Validate the transcript budget before committing staged
                    # state. An over-budget result is a failed invocation, not a
                    # successful state transaction followed by a transport error.
                    writer_tool_output_bytes = _assert_encoded_writer_result(result.value)
                    if invocation.shared_budget is not None and not invocation.shared_budget.reserve(
                        "writer_result_bytes",
                        MAX_WRITER_TOOL_RESULT_BYTES,
                        writer_tool_output_bytes,
                    ):
                        raise FlowError(
                            f"Writer tool turn returned more than {MAX_WRITER_TOOL_RESULT_BYTES} encoded result bytes"
                        )
                await _commit_staged(compiled, invocation, access, result)
                outcome = "ok"
                yield result
    except execution.InvocationBlocked as exc:
        raise FlowError(str(exc)) from None
    except FlowCancelled:
        outcome = "cancelled"
        raise
    finally:
        timer.finish(
            outcome,
            invocation.quotas,
            input_bytes=invocation.metadata.get("writer_tool_input_bytes"),
            output_bytes=writer_tool_output_bytes,
        )


# ── hook adapters ────────────────────────────────────────────────────────────


def _services(
    compiled: CompiledPackage,
    access: _StateAccess,
    *,
    lanes: Mapping[str, ModelLane],
    is_cancelled,
    context_block_error: Callable[[list[dict[str, Any]]], str | None] | None = None,
) -> HostServices:
    from .runtime import live_grants

    extension_id = compiled.extension_id

    async def owns_message(message_id: int) -> bool:
        if not access.conversation_id:
            return False
        message = await get_message_by_id(message_id)
        return bool(message and message["conversation_id"] == access.conversation_id)

    return HostServices(
        grants=lambda: live_grants(extension_id),
        read_state=access.read,
        lanes=lanes,
        is_cancelled=is_cancelled,
        context_block_error=context_block_error,
        owns_message=owns_message,
        http=_http_service(compiled, is_cancelled),
        read_asset=_asset_reader(compiled),
    )


def _http_service(compiled: CompiledPackage, is_cancelled: Callable[[], bool]) -> HttpService:
    """The extension's egress: bounded, and bound to its *live* grants.

    Always constructed, because a package with no network grant fails at
    ``inv.require`` with "permission network.request is not granted" -- which
    tells the user what to do -- rather than at the vaguer "not available in
    this invocation" an absent service produces. The absent case is still real:
    an invocation built without one (a unit test, a future context with no
    egress at all) refuses the operation outright.

    Origins and secret values are read at request time, but the set of declared
    secret names is captured from the invocation's compiled revision. Revoking
    an origin or clearing a value therefore lands on the next request without
    letting a concurrent package update change what an in-flight flow means.
    """
    from .runtime import live_grants

    extension_id = compiled.extension_id
    declared = tuple(secret.name for secret in compiled.manifest.secrets)

    def origins() -> frozenset[str]:
        return granted_origins(live_grants(extension_id))

    async def load() -> Mapping[str, str]:
        return await secrets.load_secrets(extension_id, declared)

    return HttpService(extension_id, origins=origins, secrets=load, is_cancelled=is_cancelled)


def _asset_reader(compiled: CompiledPackage) -> Callable[[str], bytes] | None:
    """Read a compiled asset from the invocation's revision, by exact key.

    Resolved against the compiled record rather than the content directory, so
    an asset request cannot outlive garbage collection or reach a file the
    compiler never selected. A path the compiled revision does not know is a
    flow error, not a filesystem miss.
    """

    def read(path: str) -> bytes:
        content = compiled.files.get(path)
        if content is None or path not in compiled.asset_types:
            raise FlowError(f"this package has no asset {path!r}")
        return content.canonical_bytes()

    return read


def _turn_lanes(source: PreCtx | PostCtx | WriterToolRequest | AuditDetectorRequest) -> dict[str, ModelLane]:
    """The two lanes an in-turn ``model.*`` operation may address.

    Reuses the turn's already-built clients so an extension call inherits the
    endpoint, credentials, model, and abort token the user configured -- and
    nothing else. Neither lane carries a prefix or a tool blob: an isolated call
    assembles its own request from the safety preamble and the flow's prompt.

    One function over every in-turn carrier because nothing in it is specific to
    any of them: each supplies the same two fields (``settings``, ``client``),
    and a second copy per entry point would be four places for the agent-lane
    resolution to drift.
    """
    settings = source.settings
    writer = ModelLane(client=source.client, model=settings["model_name"])
    agent_client, agent_model = agent_lane_from_settings(
        settings,
        writer_client=source.client,
        abort_token=getattr(source.client, "abort_token", None),
    )
    return {
        "writer": writer,
        "agent": ModelLane(client=agent_client, model=agent_model),
    }


def _cancelled(ctx: PreCtx | PostCtx) -> Callable[[], bool]:
    client = ctx.client
    return lambda: bool(getattr(client, "is_aborted", False))


def make_pre_hook(compiled: CompiledPackage, flow: Flow) -> Callable[[PreCtx], AsyncIterator[dict]]:
    """Bind a compiled flow as this extension's ``pre_pipeline`` subscription.

    A pre hook may produce trailing context blocks and state. It cannot see or
    replace a draft -- there is none yet -- and the compiler already refused
    ``draft.replace`` in this context, so the adapter has nothing to filter.
    """

    extension_id = compiled.extension_id

    async def hook(ctx: PreCtx) -> AsyncIterator[dict]:
        access = _StateAccess(
            extension_id,
            conversation_id=ctx.conversation_id,
            character_id=ctx.character_id,
        )
        invocation = Invocation(
            extension_id=extension_id,
            context=OpContext.PRE_PIPELINE,
            host=_services(
                compiled,
                access,
                lanes=_turn_lanes(ctx),
                is_cancelled=_cancelled(ctx),
                context_block_error=ctx.context_block_error,
            ),
            ctx=build_ctx(
                extension_id=extension_id,
                hook="pre_pipeline",
                granted=_granted(extension_id),
                conversation_id=ctx.conversation_id,
                card=ctx.character,
                history=ctx.history,
                last_user_message=ctx.last_user_message,
            ),
            metadata={"conversation_id": ctx.conversation_id, "hook": "pre_pipeline"},
            seed=f"pre|{ctx.conversation_id}|{len(ctx.history)}",
            scopes_in_scope=flow_scopes(flow),
        )
        async for event in _drive(
            extension_id,
            compiled,
            flow,
            invocation,
            access,
            "pre_pipeline",
            generation=ctx.runtime_generation,
        ):
            yield event

    return hook


def make_post_hook(
    compiled: CompiledPackage,
    flow: Flow,
    stage: HookStage,
) -> Callable[[PostCtx], AsyncIterator[dict]]:
    """Bind a compiled flow as this extension's ``post_pipeline`` subscription."""

    extension_id = compiled.extension_id

    async def hook(ctx: PostCtx) -> AsyncIterator[dict]:
        access = _StateAccess(
            extension_id,
            conversation_id=ctx.conversation_id,
            character_id=ctx.character_id,
            pending_message=True,
        )
        invocation = Invocation(
            extension_id=extension_id,
            context=OpContext.POST_TRANSFORM if stage is HookStage.TRANSFORM else OpContext.POST_OBSERVE,
            host=_services(
                compiled,
                access,
                lanes=_turn_lanes(ctx),
                is_cancelled=_cancelled(ctx),
            ),
            ctx=build_ctx(
                extension_id=extension_id,
                hook="post_pipeline",
                granted=_granted(extension_id),
                conversation_id=ctx.conversation_id,
                card=ctx.character,
                history=ctx.history,
                last_user_message=ctx.effective_msg,
                draft=ctx.draft,
                direction=ctx.direction,
            ),
            metadata={
                "conversation_id": ctx.conversation_id,
                "hook": "post_pipeline",
                "stage": stage.value,
            },
            seed=f"post|{ctx.conversation_id}|{len(ctx.history)}",
            scopes_in_scope=flow_scopes(flow) | {"message"},
        )
        async for event in _drive(
            extension_id,
            compiled,
            flow,
            invocation,
            access,
            "post_pipeline",
            generation=ctx.runtime_generation,
        ):
            yield event

    return hook


def _granted(extension_id: str) -> frozenset[tuple[str, str | None]]:
    from .runtime import live_grants

    return live_grants(extension_id)


async def _drive(
    extension_id: str,
    compiled: CompiledPackage,
    flow: Flow,
    invocation: Invocation,
    access: _StateAccess,
    hook_name: str,
    *,
    generation: int,
) -> AsyncIterator[dict]:
    """Run a hook invocation and translate its output into control events.

    A failure aborts this extension's invocation and is logged; it never aborts
    the core turn, and it never discards an earlier extension's successful
    transform. The status line is cleared in ``finally`` whether the flow
    succeeded, failed, or was cancelled -- a spinner left behind by a crash
    would be indistinguishable from work still running.
    """
    emitted_status = False
    try:
        async for event in _invoke(
            compiled=compiled,
            flow=flow,
            invocation=invocation,
            access=access,
            entry_point=hook_name,
        ):
            if isinstance(event, FlowResult):
                for control in _result_events(compiled, event, generation, seed=invocation.seed):
                    yield control
                continue
            emitted_status = True
            yield {
                "event": EV_EXTENSION_STATUS,
                "data": {"extension_id": extension_id, "text": event["text"]},
            }
    except FlowCancelled:
        logger.info("extension %r %s invocation cancelled", extension_id, hook_name)
    except FlowError as exc:
        logger.warning("extension %r %s invocation failed: %s", extension_id, hook_name, exc)
    except Exception:
        logger.exception("extension %r %s invocation raised", extension_id, hook_name)
    finally:
        if emitted_status:
            yield {
                "event": EV_EXTENSION_STATUS,
                "data": {"extension_id": extension_id, "text": ""},
            }


def _result_events(compiled: CompiledPackage, result: FlowResult, generation: int, *, seed: str = ""):
    """The control and public events one committed invocation produces."""
    extension_id = compiled.extension_id
    effects = result.effects
    for block in effects.context_blocks:
        yield {"type": EV_CONTEXT_BLOCK, "extension_id": extension_id, **block}
    if effects.draft is not None:
        yield {"type": EV_DRAFT_REPLACED, "draft": effects.draft}
    for payload in artifact_payloads(compiled, effects, seed):
        # The assistant row does not exist yet, so the attachment rides the
        # pipeline result exactly as a message-state write does, and the bridge
        # persists it with that row. Its ``workflow_id``/``source`` pairing was
        # stamped by the host, which is what the bridge validates before staging.
        yield {"type": EV_ATTACH_ARTIFACT, "attachment": payload}
    message_state = effects.state.get("message")
    if message_state:
        # The assistant row does not exist yet, so the write rides the pipeline
        # result and is committed with that row. A staged delete has nothing to
        # delete on a message being created, so it collapses to "absent".
        payload = {path: value for path, value in message_state.items() if value is not DELETED}
        if payload:
            yield {"type": EV_SET_MESSAGE_STATE, "state": payload}
    if effects.invalidations or effects.toasts:
        yield {
            "event": EV_EXTENSION_EFFECTS,
            "data": _effect_payload(extension_id, effects, generation),
        }


# ── on-demand actions ────────────────────────────────────────────────────────


async def run_action(
    *,
    compiled: CompiledPackage,
    action_name: str,
    flow: Flow,
    action_input: Mapping[str, Any],
    input_schema: Mapping[str, Any] | None,
    output_schema: Mapping[str, Any] | None,
    conversation_id: str | None,
    character_id: str | None,
    lanes: Mapping[str, ModelLane],
    is_cancelled: Callable[[], bool],
    ctx_fields: Mapping[str, Any],
    generation: int,
) -> dict[str, Any]:
    """Run one named action and return its fixed effect envelope.

    Unlike a hook, an action's failure is the user's business: it raises
    :class:`~.errors.FlowError` with a sanitized message the route turns into a
    4xx. Status lines have nowhere to stream on a plain JSON response and are
    dropped, which is why ``ui.status`` is progress rather than a return
    channel.

    ``character_id`` is whatever the *route* resolved -- the conversation's card
    for an ordinary action, or the card a validated ``card_id`` named for a
    library-scoped one. Resolving it there rather than here is what makes the
    dual-grant check a precondition of the invocation instead of a rule the
    invocation has to remember: this function receives a card that was already
    permitted.
    """
    extension_id = compiled.extension_id
    assert_json_bounds(dict(action_input), what="action input")
    if input_schema is not None:
        reason = parse_schema(input_schema, what="input_schema").validate(dict(action_input))
        if reason is not None:
            raise FlowError(f"action input does not match its declared schema: {reason}")

    access = _StateAccess(extension_id, conversation_id=conversation_id, character_id=character_id)
    invocation = Invocation(
        extension_id=extension_id,
        context=OpContext.ACTION,
        host=_services(compiled, access, lanes=lanes, is_cancelled=is_cancelled),
        ctx=build_ctx(
            extension_id=extension_id,
            hook="action",
            granted=_granted(extension_id),
            conversation_id=conversation_id,
            **ctx_fields,
        ),
        action_input=dict(action_input),
        metadata={"conversation_id": conversation_id, "hook": "action"},
        seed=f"action|{conversation_id or ''}|{generation}",
        scopes_in_scope=flow_scopes(flow),
        output_schema=parse_schema(output_schema, what="output_schema") if output_schema is not None else None,
    )

    result: FlowResult | None = None
    async for event in _invoke(
        compiled=compiled,
        flow=flow,
        invocation=invocation,
        access=access,
        entry_point=f"action {action_name}",
    ):
        if isinstance(event, FlowResult):
            result = event
    assert result is not None
    return _effect_payload(
        extension_id,
        result.effects,
        generation,
        data=result.value,
        conversation_id=conversation_id,
    )


# ── artifact recovery ────────────────────────────────────────────────────────


def _decode_metadata(raw: Any) -> Mapping[str, Any]:
    """Read a stored ``generation_metadata`` column into a mapping."""
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


async def _run_recovery(
    compiled: CompiledPackage,
    flow: Flow,
    *,
    entry_point: str,
    conversation_id: str,
    message_id: int,
    stored_metadata: Any,
    settings: Mapping[str, Any],
    client: Any,
    seed: str,
) -> list[dict[str, Any]]:
    """Run one recovery flow on the revision captured by the registry snapshot.

    The revision binding is the whole point of this function. The stored
    metadata says which revision produced the bytes, but the flow that runs is
    the one captured when this request resolved its registry snapshot, under
    live grants. Orb does not resurrect the producing revision or its old
    permission set. The stored payload is validated against the captured
    revision's ``recovery_input_schema`` first.
    """
    extension_id = compiled.extension_id
    declared = compiled.manifest.artifact_flows
    schema = None
    if declared is not None and declared.recovery_input_schema is not None:
        schema = parse_schema(declared.recovery_input_schema, what="recovery_input_schema")
    recovery = artifacts.recovery_input(_decode_metadata(stored_metadata), schema=schema)

    access = _StateAccess(extension_id, conversation_id=conversation_id, character_id=None)
    agent_client, agent_model = agent_lane_from_settings(settings, writer_client=client)
    invocation = Invocation(
        extension_id=extension_id,
        context=OpContext.RECOVERY,
        host=_services(
            compiled,
            access,
            lanes={
                "writer": ModelLane(client=client, model=settings["model_name"]),
                "agent": ModelLane(client=agent_client, model=agent_model),
            },
            is_cancelled=lambda: bool(getattr(client, "is_aborted", False)),
        ),
        ctx=build_ctx(
            extension_id=extension_id,
            hook="recovery",
            granted=_granted(extension_id),
            conversation_id=conversation_id,
            message_id=message_id,
        ),
        action_input=recovery,
        metadata={
            "conversation_id": conversation_id,
            "hook": "recovery",
            "message_id": message_id,
        },
        # Regenerate replays the original seed and reroll receives a fresh one,
        # so a deterministic flow reproduces its own bytes on one path and
        # deliberately does not on the other.
        seed=seed,
        scopes_in_scope=flow_scopes(flow),
    )
    result: FlowResult | None = None
    async for event in _invoke(
        compiled=compiled,
        flow=flow,
        invocation=invocation,
        access=access,
        entry_point=entry_point,
    ):
        if isinstance(event, FlowResult):
            result = event
    assert result is not None
    payloads = artifact_payloads(compiled, result.effects, seed)
    if not payloads:
        raise FlowError("the recovery flow produced no artifact")
    return payloads


def make_regenerate(
    compiled: CompiledPackage,
    flow: Flow,
) -> Callable[[RegenCtx, dict], Awaitable[list[dict[str, Any]]]]:
    """Bind a compiled flow as this extension's ``regenerate`` handler.

    Regenerate reuses the original attachment's recorded seed, so a flow whose
    output depends on ``random.*`` reproduces the same draw. What it does *not*
    reuse is the producing revision: the flow/schema/assets come from the
    request's registry snapshot and grants remain live.
    """

    async def regenerate(ctx: RegenCtx, _body: dict) -> list[dict[str, Any]]:
        original = ctx.original_attachment
        return await _run_recovery(
            compiled,
            flow,
            entry_point="regenerate",
            conversation_id=ctx.conversation_id,
            message_id=ctx.message_id,
            stored_metadata=original.get("generation_metadata"),
            settings=ctx.settings,
            client=ctx.client,
            seed=str(original.get("seed") or ""),
        )

    return regenerate


def make_reroll(
    compiled: CompiledPackage,
    flow: Flow,
) -> Callable[[RerollGenCtx, dict, str], Awaitable[bytes]]:
    """Bind a compiled flow as this extension's ``reroll_gen`` handler.

    The framework hands it the stored parameters and a *new* seed and expects
    bytes back; it owns the sibling row, the byte budget, and the rehydrate
    path. The contract is exactly one attachment: accepting two and silently
    discarding one would persist provenance for bytes other than those returned.
    """

    async def reroll(ctx: RerollGenCtx, params: dict, seed: str) -> bytes:
        payloads = await _run_recovery(
            compiled,
            flow,
            entry_point="reroll_gen",
            conversation_id=ctx.conversation_id,
            message_id=ctx.message_id,
            stored_metadata=params or ctx.original_attachment.get("generation_metadata"),
            settings=ctx.settings,
            client=ctx.client,
            seed=seed,
        )
        if len(payloads) != 1:
            raise FlowError("the reroll recovery flow must produce exactly one artifact")
        params.clear()
        params.update(payloads[0]["generation_metadata"])
        return payloads[0]["data"]

    return reroll


# ── host-generated state writes ──────────────────────────────────────────────


async def write_view_state(
    entry: Any,
    updates: Mapping[str, Mapping[str, Any]],
    *,
    conversation_id: str | None,
) -> int:
    """Commit a bound form's draft into the extension's own slots.

    Shares the invocation transaction's machinery rather than reimplementing
    it: the same ``_StateAccess``, the same lock order, the same
    ``commit_extension_state`` with its in-transaction validation hook, and the
    same per-scope size cap. What it does *not* share is a flow -- there is no
    package code in this path at all, which is why a config view can save
    settings without the package declaring an action.

    Raises :class:`PermissionError` for an ungranted scope and
    :class:`~.errors.FlowError` for anything else, both of which the route maps
    to a status.
    """
    from .runtime import live_grants

    granted = live_grants(entry.id)
    conv = await get_conversation(conversation_id) if conversation_id else None
    character_id = conv.get("character_card_id") if conv else None
    access = _StateAccess(entry.id, conversation_id=conversation_id, character_id=character_id)

    merged: dict[str, dict[str, Any]] = {}
    current: dict[str, Mapping[str, Any]] = {}
    async with AsyncExitStack() as stack:
        await _locks(stack, access, frozenset(updates) & set(LOCKED_SCOPES))
        for scope, values in updates.items():
            if scope not in LOCKED_SCOPES:
                raise FlowError(f"a view cannot write {scope!r} state")
            if (Capability.STATE_WRITE.value, scope) not in granted:
                raise PermissionError(f"this extension has not been granted state.write on {scope!r}")
            if scope not in access.available(frozenset({scope})):
                raise FlowError(f"there is no {scope} in scope for this view")
            stored = dict(await access.read(scope))
            current[scope] = stored
            stored.update(assert_json_bounds(dict(values), what=f"the {scope} state written by this view"))
            _assert_slot_bytes(stored, scope)
            merged[scope] = stored
        await commit_extension_state(
            entry.id,
            merged,
            conversation_id=access.conversation_id,
            character_id=access.character_id,
            validate=lambda: _assert_still_granted(entry.id, merged),
        )
    from .runtime import current_state

    return current_state().generation


def _assert_slot_bytes(slot: Mapping[str, Any], scope: str) -> None:
    size = len(canonical_json_bytes(dict(slot)))
    if size > MAX_STATE_BYTES_PER_SCOPE:
        raise FlowError(f"the extension's {scope} state is {size} bytes, over the {MAX_STATE_BYTES_PER_SCOPE} byte limit")


def _assert_still_granted(extension_id: str, updates: Mapping[str, Any]) -> None:
    """Re-check grants inside the write transaction, as a flow commit does.

    A concurrent permission revocation must not slip between the check above
    and these writes; ``commit_extension_state`` runs this after
    ``BEGIN IMMEDIATE``, so it cannot.
    """
    from .runtime import live_grants

    granted = live_grants(extension_id)
    for scope in updates:
        if (Capability.STATE_WRITE.value, scope) not in granted:
            raise PermissionError(f"this extension has not been granted state.write on {scope!r}")


# ── publishing ───────────────────────────────────────────────────────────────


def hook_bindings(compiled: CompiledPackage, blocked: Sequence[str]):
    """Yield ``(HookType, callable, stage)`` for every publishable hook.

    A hook whose entry point is blocked -- under-granted, or reaching an
    operation this build cannot execute -- yields nothing. It stays visible in
    the package's diagnostic instead of being published and failing halfway
    through ordinary use.
    """
    manifest = compiled.manifest
    flows = compiled.flows
    if manifest.hooks.pre_pipeline and "hook pre_pipeline" not in blocked:
        flow = flows.get(manifest.hooks.pre_pipeline.flow)
        if flow is not None:
            yield (
                HookType.PRE_PIPELINE,
                make_pre_hook(compiled, flow),
                HookStage.TRANSFORM,
            )
    if manifest.hooks.post_pipeline and "hook post_pipeline" not in blocked:
        flow = flows.get(manifest.hooks.post_pipeline.flow)
        if flow is not None:
            stage = HookStage.TRANSFORM if manifest.hooks.post_pipeline.stage == "transform" else HookStage.OBSERVE
            yield (
                HookType.POST_PIPELINE,
                make_post_hook(compiled, flow, stage),
                stage,
            )
    yield from artifact_bindings(compiled, blocked)


def artifact_bindings(compiled: CompiledPackage, blocked: Sequence[str]):
    """Yield the regenerate/reroll pair, or nothing at all.

    Nothing at all is the important case. The registry's artifact mandate is
    all-or-nothing -- a record declaring ``produces_artifacts`` without both
    hooks fails the *whole* overlay swap -- so a package whose recovery flows
    are blocked must publish neither the hooks nor the declaration.
    :func:`publishes_artifacts` is the one predicate both this and the record
    builder ask, so the two cannot disagree about a package that would
    otherwise take every other extension down with it at startup.
    """
    manifest = compiled.manifest
    flows = compiled.flows
    if not publishes_artifacts(manifest, flows, blocked):
        return
    declared = manifest.artifact_flows
    assert declared is not None
    yield (
        HookType.REGENERATE,
        make_regenerate(compiled, flows[declared.regenerate]),
        HookStage.TRANSFORM,
    )
    yield (
        HookType.REROLL_GEN,
        make_reroll(compiled, flows[declared.reroll_gen]),
        HookStage.TRANSFORM,
    )


def publishes_artifacts(manifest, flows: Mapping[str, Flow], blocked: Sequence[str]) -> bool:
    """Whether this revision can honor the artifact contract end to end."""
    declared = manifest.artifact_flows
    if not manifest.produces_artifacts or declared is None:
        return False
    if "artifact flows" in blocked:
        return False
    return declared.regenerate in flows and declared.reroll_gen in flows


def blocked_by_build(flows: Mapping[str, Flow]) -> dict[str, list[str]]:
    """Per-flow operations this build cannot execute, keyed by flow path."""
    return {path: ops for path, flow in flows.items() if (ops := unimplemented_operations(flow))}
