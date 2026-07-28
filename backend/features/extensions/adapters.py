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

import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

from ...core import (
    conversation_stream_lock,
    workflow_character_state_lock,
    workflow_config_lock,
    workflow_state_lock,
)
from ...database import (
    commit_extension_state,
    get_conversation,
    get_message_by_id,
    get_workflow_character_state,
    get_workflow_config,
    get_workflow_state,
    switch_to_branch,
    update_character_card,
)
from ...inference import agent_lane_from_settings
from ...workflows.contracts import (
    EV_CONTEXT_BLOCK,
    EV_DRAFT_REPLACED,
    EV_SET_MESSAGE_STATE,
    HookStage,
    HookType,
    PostCtx,
    PreCtx,
)
from . import execution, telemetry
from .contracts import (
    Capability,
    EffectEnvelope,
    Flow,
    OpContext,
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
from .limits import MAX_STATE_BYTES_PER_SCOPE
from .values import assert_json_bounds

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


async def _commit_state(
    access: _StateAccess,
    effects: StagedEffects,
    current: Mapping[str, Mapping[str, Any]],
    validate: Callable[[], None],
) -> None:
    """Fold staged writes and commit every database-backed scope atomically."""
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
    if not updates:
        validate()
        return
    await commit_extension_state(
        access.extension_id,
        updates,
        conversation_id=access.conversation_id,
        character_id=access.character_id,
        validate=validate,
    )


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
                {"resource": "conversation.messages", "conversation_id": conversation_id},
                {"resource": "conversation.director", "conversation_id": conversation_id},
                {"resource": "conversation.direction_notes", "conversation_id": conversation_id},
            ]
        )
    listed.extend({"resource": "extension.view", "extension_id": extension_id, "view": view} for view in effects.invalidations)
    return listed


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


async def _commit_card_tags(effects: StagedEffects) -> None:
    """Write the staged tag list through the ordinary card update path.

    Through ``update_character_card`` rather than straight into the column,
    because that is where the shared normalizer runs -- the same one the
    character API goes through. Writing the column directly would be the second
    tag-writing path, and the whole reason ``card.tags.set`` is admissible is
    that there is only one.

    Committed *before* the namespaced state write. If the pair cannot both
    land, the survivable half is "tags written, not yet recorded as
    classified": the next sweep re-classifies that card. The reverse leaves a
    card marked done that never got its tags, which no later run would revisit.
    """
    if effects.card_tags is None:
        return
    card_id, tags = effects.card_tags
    if await update_character_card(card_id, {"tags": tags}) is None:
        raise FlowError("the character card this action targets no longer exists")


async def _commit_branch(effects: StagedEffects, conversation_id: str | None) -> None:
    """Activate the staged branch through the same helper /switch-branch uses.

    Called while the conversation stream lock is still held, so it serializes
    against a running turn exactly as the built-in button does. The ownership
    re-check inside ``switch_to_branch`` is redundant under that lock and kept
    anyway: it is the storage-level guarantee, and a lock is a process-level
    one.
    """
    if effects.branch_activation is None:
        return
    if not conversation_id or not await switch_to_branch(conversation_id, effects.branch_activation):
        raise FlowError("the selected message is not part of this conversation")


async def _invoke(
    *,
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
                _validate_staged_effects(invocation, result)
                await _commit_card_tags(result.effects)
                await _commit_state(
                    access,
                    result.effects,
                    invocation.state_snapshots,
                    lambda: _validate_staged_effects(invocation, result),
                )
                await _commit_branch(result.effects, conversation_id)
                outcome = "ok"
                yield result
    except execution.InvocationBlocked as exc:
        raise FlowError(str(exc)) from None
    except FlowCancelled:
        outcome = "cancelled"
        raise
    finally:
        timer.finish(outcome, invocation.quotas)


# ── hook adapters ────────────────────────────────────────────────────────────


def _services(
    extension_id: str,
    access: _StateAccess,
    *,
    lanes: Mapping[str, ModelLane],
    is_cancelled,
    context_block_error: Callable[[list[dict[str, Any]]], str | None] | None = None,
) -> HostServices:
    from .runtime import live_grants

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
    )


def _turn_lanes(ctx: PreCtx | PostCtx) -> dict[str, ModelLane]:
    """The two lanes a hook's ``model.*`` operation may address.

    Reuses the turn's already-built clients so an extension call inherits the
    endpoint, credentials, and model the user configured -- and nothing else.
    Neither lane carries a prefix or a tool blob: an isolated call assembles its
    own request from the safety preamble and the flow's prompt.
    """
    settings = ctx.settings
    writer = ModelLane(client=ctx.client, model=settings["model_name"])
    agent_client, agent_model = agent_lane_from_settings(
        settings,
        writer_client=ctx.client,
        abort_token=getattr(ctx.client, "abort_token", None),
    )
    return {"writer": writer, "agent": ModelLane(client=agent_client, model=agent_model)}


def _cancelled(ctx: PreCtx | PostCtx) -> Callable[[], bool]:
    client = ctx.client
    return lambda: bool(getattr(client, "is_aborted", False))


def make_pre_hook(extension_id: str, flow: Flow) -> Callable[[PreCtx], AsyncIterator[dict]]:
    """Bind a compiled flow as this extension's ``pre_pipeline`` subscription.

    A pre hook may produce trailing context blocks and state. It cannot see or
    replace a draft -- there is none yet -- and the compiler already refused
    ``draft.replace`` in this context, so the adapter has nothing to filter.
    """

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
                extension_id,
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
            flow,
            invocation,
            access,
            "pre_pipeline",
            generation=ctx.runtime_generation,
        ):
            yield event

    return hook


def make_post_hook(extension_id: str, flow: Flow, stage: HookStage) -> Callable[[PostCtx], AsyncIterator[dict]]:
    """Bind a compiled flow as this extension's ``post_pipeline`` subscription."""

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
                extension_id,
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
        async for event in _invoke(flow=flow, invocation=invocation, access=access, entry_point=hook_name):
            if isinstance(event, FlowResult):
                for control in _result_events(extension_id, event, generation):
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


def _result_events(extension_id: str, result: FlowResult, generation: int):
    """The control and public events one committed invocation produces."""
    effects = result.effects
    for block in effects.context_blocks:
        yield {"type": EV_CONTEXT_BLOCK, "extension_id": extension_id, **block}
    if effects.draft is not None:
        yield {"type": EV_DRAFT_REPLACED, "draft": effects.draft}
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
    extension_id: str,
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
    assert_json_bounds(dict(action_input), what="action input")
    if input_schema is not None:
        reason = parse_schema(input_schema, what="input_schema").validate(dict(action_input))
        if reason is not None:
            raise FlowError(f"action input does not match its declared schema: {reason}")

    access = _StateAccess(extension_id, conversation_id=conversation_id, character_id=character_id)
    invocation = Invocation(
        extension_id=extension_id,
        context=OpContext.ACTION,
        host=_services(extension_id, access, lanes=lanes, is_cancelled=is_cancelled),
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
    async for event in _invoke(flow=flow, invocation=invocation, access=access, entry_point=f"action {action_name}"):
        if isinstance(event, FlowResult):
            result = event
    assert result is not None
    return _effect_payload(extension_id, result.effects, generation, data=result.value, conversation_id=conversation_id)


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


def hook_bindings(manifest, flows: Mapping[str, Flow], blocked: Sequence[str]):
    """Yield ``(HookType, callable, stage)`` for every publishable hook.

    A hook whose entry point is blocked -- under-granted, or reaching an
    operation this build cannot execute -- yields nothing. It stays visible in
    the package's diagnostic instead of being published and failing halfway
    through ordinary use.
    """
    if manifest.hooks.pre_pipeline and "hook pre_pipeline" not in blocked:
        flow = flows.get(manifest.hooks.pre_pipeline.flow)
        if flow is not None:
            yield (
                HookType.PRE_PIPELINE,
                make_pre_hook(manifest.id, flow),
                HookStage.TRANSFORM,
            )
    if manifest.hooks.post_pipeline and "hook post_pipeline" not in blocked:
        flow = flows.get(manifest.hooks.post_pipeline.flow)
        if flow is not None:
            stage = HookStage.TRANSFORM if manifest.hooks.post_pipeline.stage == "transform" else HookStage.OBSERVE
            yield (
                HookType.POST_PIPELINE,
                make_post_hook(manifest.id, flow, stage),
                stage,
            )


def blocked_by_build(flows: Mapping[str, Flow]) -> dict[str, list[str]]:
    """Per-flow operations this build cannot execute, keyed by flow path."""
    return {path: ops for path, flow in flows.items() if (ops := unimplemented_operations(flow))}
