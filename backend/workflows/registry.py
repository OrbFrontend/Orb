"""Workflow registry, registration entry point, and per-workflow storage.

The registry is a process-local mapping of workflow ids to ``Workflow``
records. Iteration order matches registration order: the dict preserves
insertion order, and reassignment to an existing key keeps the original
position. Hooks are attached separately through ``subscribe`` and live
on the owner record's ``subscriptions`` list.

Storage wrappers (``get_workflow_state`` etc.) are thin awaiting wrappers
over ``backend.database`` so the toolkit has a single namespace for both
core reads and workflow-scoped reads. ``get_workflow_config`` is the one
exception that adds behavior: it falls back to the workflow's
``config_defaults`` when the DB slot is empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

from ..database import (
    get_workflow_character_state as _db_get_workflow_character_state,
)
from ..database import (
    get_workflow_config as _db_get_workflow_config,
)
from ..database import (
    get_workflow_message_state as _db_get_workflow_message_state,
)
from ..database import (
    get_workflow_state as _db_get_workflow_state,
)
from ..database import (
    set_workflow_character_state as _db_set_workflow_character_state,
)
from ..database import (
    set_workflow_config as _db_set_workflow_config,
)
from ..database import (
    set_workflow_message_state as _db_set_workflow_message_state,
)
from ..database import (
    set_workflow_state as _db_set_workflow_state,
)
from ..inference import (
    BUILTIN_TOOL_NAMES,
    STANDALONE_TOOLS,
    TOOLS,
    register_tool,
)
from .contracts import HookType, ToolSpec


@dataclass
class Workflow:
    """Per-id workflow metadata.

    ``produces_artifacts=True`` is a contract: the workflow MUST also
    ``subscribe`` to both ``REGENERATE`` and ``REROLL_GEN`` -- ``subscribe``
    blocks artifact-hook bindings on workflows that disclaim the contract,
    and ``finalize_registry`` blocks the process from starting when one
    side is declared without the other.
    """

    id: str
    display_name: str
    tools: list[ToolSpec] = field(default_factory=list)
    config_defaults: dict = field(default_factory=dict)
    config_schema: Optional[dict] = None
    produces_artifacts: bool = False
    subscriptions: list["Subscription"] = field(default_factory=list)


@dataclass(frozen=True)
class Subscription:
    """A workflow's binding into one pipeline hook slot.

    ``priority`` only matters for fan-out slots (``PRE_PIPELINE``,
    ``POST_PIPELINE``); single-dispatch slots are resolved by workflow id
    and ignore it.
    """

    hook_type: HookType
    callable: Callable
    priority: int = 0
    workflow_id: str = ""


class ToolNameCollision(Exception):
    """Raised when a workflow tries to claim a tool name that is already
    taken by a built-in pass or by another registered workflow."""


class WorkflowMandateError(ValueError):
    """Raised by ``finalize_registry`` when a ``produces_artifacts=True``
    workflow lacks ``REGENERATE`` and/or ``REROLL_GEN``. Failing at import
    rather than on the first regen click avoids shipping a half-bound
    artifact workflow into production."""


_WORKFLOWS_BY_ID: dict[str, Workflow] = {}


def register_workflow(w: Workflow) -> None:
    """Register or replace a workflow. Idempotent on ``w.id``.

    Tool diff against any prior registration of the same id:
      - names in new only -> ``register_tool`` is called fresh
      - names in both -> ``register_tool`` overwrites schema/choice and
        toggles the standalone bit symmetrically
      - names in old only -> removed from ``TOOLS`` and
        ``STANDALONE_TOOLS`` so the workflow's declaration stays the
        single source of truth for what it owns

    Validation order: built-in name reservation first (more informative
    error wins over a cross-workflow collision on the same name), then
    cross-workflow collision on names this registration is newly claiming.
    Both raise ``ToolNameCollision`` before any state mutation, so a
    rejected call leaves the registry, ``TOOLS``, and ``STANDALONE_TOOLS``
    exactly as they were. Re-registration preserves the original
    insertion position so manifest ordering stays stable across reloads.
    """
    for spec in w.tools:
        if spec.name in BUILTIN_TOOL_NAMES:
            raise ToolNameCollision(f"workflow {w.id!r} cannot claim built-in tool name {spec.name!r}")

    old = _WORKFLOWS_BY_ID.get(w.id)
    old_tool_names = {t.name for t in old.tools} if old else frozenset()
    new_tool_names = {t.name for t in w.tools}

    newly_claimed = new_tool_names - old_tool_names
    for name in newly_claimed:
        for other in _WORKFLOWS_BY_ID.values():
            if other.id == w.id:
                continue
            if any(t.name == name for t in other.tools):
                raise ToolNameCollision(f"workflow {w.id!r} cannot claim tool name {name!r} owned by workflow {other.id!r}")

    for spec in w.tools:
        register_tool(spec.name, spec.schema, spec.choice, standalone=spec.standalone)

    for orphan in old_tool_names - new_tool_names:
        TOOLS.pop(orphan, None)
        STANDALONE_TOOLS.discard(orphan)

    _WORKFLOWS_BY_ID[w.id] = w


def subscribe(
    workflow_id: str,
    hook_type: HookType,
    fn: Callable,
    *,
    priority: int = 0,
) -> None:
    record = _WORKFLOWS_BY_ID.get(workflow_id)
    if record is None:
        raise LookupError(f"subscribe: workflow {workflow_id!r} not registered")
    if any(s.hook_type is hook_type for s in record.subscriptions):
        raise ValueError(f"workflow {workflow_id!r} already has a {hook_type.value} subscription")
    if hook_type in (HookType.REGENERATE, HookType.REROLL_GEN) and not record.produces_artifacts:
        raise ValueError(f"workflow {workflow_id!r} cannot subscribe to {hook_type.value} without produces_artifacts=True")
    record.subscriptions.append(Subscription(hook_type, fn, priority, workflow_id))


def iter_subscriptions(hook_type: HookType) -> list[Subscription]:
    """Return subscriptions of ``hook_type`` sorted by priority ascending.

    Tie-break is registration order: ``dict`` insertion order plus
    Python's stable sort preserves it without an explicit secondary key.
    """
    subs = [s for w in _WORKFLOWS_BY_ID.values() for s in w.subscriptions if s.hook_type is hook_type]
    subs.sort(key=lambda s: s.priority)
    return subs


def get_subscription(workflow_id: str, hook_type: HookType) -> Optional[Subscription]:
    """Return the workflow's subscription for ``hook_type``, or None.

    Collapses "unregistered" and "no binding" into one None -- the routes
    that use this (regenerate, reroll_gen) treat both as 404 anyway.
    """
    record = _WORKFLOWS_BY_ID.get(workflow_id)
    if record is None:
        return None
    return next((s for s in record.subscriptions if s.hook_type is hook_type), None)


def workflow_has_hook(w: Workflow, hook_type: HookType) -> bool:
    return any(s.hook_type is hook_type for s in w.subscriptions)


def finalize_registry() -> None:
    """Validate that every ``produces_artifacts=True`` workflow has both
    ``REGENERATE`` and ``REROLL_GEN`` subscriptions.

    Invoke at the bottom of any module that completes a workflow's wiring
    -- this is the only hook that fails import on a partially-bound
    artifact workflow rather than deferring the crash to the first regen
    click.
    """
    for w in _WORKFLOWS_BY_ID.values():
        if not w.produces_artifacts:
            continue
        missing: list[str] = []
        if not workflow_has_hook(w, HookType.REGENERATE):
            missing.append("regenerate")
        if not workflow_has_hook(w, HookType.REROLL_GEN):
            missing.append("reroll_gen")
        if missing:
            raise WorkflowMandateError(
                f"workflow {w.id!r} declares produces_artifacts=True but lacks subscriptions: {', '.join(missing)}"
            )


def list_workflows() -> list[Workflow]:
    """Return a shallow copy of the registry in registration order."""
    return list(_WORKFLOWS_BY_ID.values())


def get_workflow(workflow_id: str) -> Optional[Workflow]:
    """Look up a workflow by id, or None if not registered."""
    return _WORKFLOWS_BY_ID.get(workflow_id)


async def get_workflow_state(conv_id: str, workflow_id: str) -> dict | None:
    """Return the workflow's per-conversation slot, or None if empty."""
    return await _db_get_workflow_state(conv_id, workflow_id)


async def set_workflow_state(conv_id: str, workflow_id: str, payload: dict | None) -> None:
    """Write the workflow's per-conversation slot. None removes it."""
    await _db_set_workflow_state(conv_id, workflow_id, payload)


async def get_workflow_message_state(message_id: int, workflow_id: str) -> dict | None:
    """Return the workflow's per-message slot, or None if empty."""
    return await _db_get_workflow_message_state(message_id, workflow_id)


async def set_workflow_message_state(message_id: int, workflow_id: str, payload: dict | None) -> None:
    """Write the workflow's per-message slot. None removes it."""
    await _db_set_workflow_message_state(message_id, workflow_id, payload)


async def get_workflow_character_state(character_id: str, workflow_id: str) -> dict | None:
    return await _db_get_workflow_character_state(character_id, workflow_id)


async def set_workflow_character_state(character_id: str, workflow_id: str, payload: dict | None) -> None:
    await _db_set_workflow_character_state(character_id, workflow_id, payload)


async def get_workflow_config(workflow_id: str) -> dict:
    """Return the workflow's global config slot.

    Falls back to the workflow's ``config_defaults`` (fresh copy) when the
    persisted slot is empty so callers can read into a populated dict
    without an existence check. An unregistered ``workflow_id`` with an
    empty slot returns an empty dict.

    Callers doing read-then-write must hold ``workflow_config_lock()``
    across the full RMW window so the value observed here -- whether from
    the persisted slot or the ``config_defaults`` fallback -- still
    matches the slot when ``set_workflow_config`` writes it back.
    """
    raw = await _db_get_workflow_config(workflow_id)
    if raw:
        return raw
    w = _WORKFLOWS_BY_ID.get(workflow_id)
    if w is not None:
        return dict(w.config_defaults)
    return {}


async def set_workflow_config(workflow_id: str, payload: dict) -> None:
    """Write the workflow's global config slot. Empty dict clears it.

    Caller must hold ``workflow_config_lock()`` across the read-then-write
    the payload was computed from. Direct use without the lock is safe for
    blind-replace writes; RMW sequences (``get_workflow_config`` -> mutate
    -> ``set_workflow_config``) silently lose writes under contention
    because the read happens in a separate transaction outside the lock.
    """
    await _db_set_workflow_config(workflow_id, payload)


def overlay_enable_tools(
    base: Mapping[str, bool],
    contribution: set[str] | Mapping[str, bool] | None,
) -> dict[str, bool]:
    """Return a fresh mutable dict copy of *base* with *contribution*'s
    True entries merged in. Mirrors the orchestrator merge semantics:
    True wins, False is ignored. None or an empty contribution returns a
    fresh copy of *base* unchanged.

    Accepts any ``Mapping`` for *base* including a ``MappingProxyType``;
    the return is always a plain ``dict`` the caller may mutate freely
    (e.g. to pass to ``forced_tool_call``'s ``enabled_tools=`` argument).
    Contribution may be a ``set`` (presence = enable) or a ``Mapping``
    (True entries kept, False entries dropped). The orchestrator does the
    logged-warning at its own merge site; this helper trusts the caller.
    """
    result = dict(base)
    if contribution is None:
        return result
    if isinstance(contribution, (set, frozenset)):
        for name in contribution:
            result[name] = True
    else:
        for name, enabled in contribution.items():
            if enabled:
                result[name] = True
    return result
