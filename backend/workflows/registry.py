"""Discover and register workflows and expose their scoped storage helpers."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, overload

from ..database import get_workflow_character_state as _db_get_workflow_character_state
from ..database import get_workflow_config as _db_get_workflow_config
from ..database import get_workflow_message_state as _db_get_workflow_message_state
from ..database import get_workflow_state as _db_get_workflow_state
from ..database import set_workflow_character_state as _db_set_workflow_character_state
from ..database import set_workflow_config as _db_set_workflow_config
from ..database import set_workflow_message_state as _db_set_workflow_message_state
from ..database import set_workflow_state as _db_set_workflow_state
from ..prompting.tool_catalog import BUILTIN_TOOL_NAMES, register_tool, remove_tool
from .contracts import (
    ExportHook,
    HookType,
    OnDemandHook,
    PostHook,
    PreHook,
    QueryHook,
    RegenHook,
    RerollGenHook,
    ToolSpec,
    UploadHook,
)


@dataclass
class Workflow:
    """Per-workflow metadata and subscriptions.

    A plug-in declares its hooks in ``subscriptions``; ``register_workflow``
    validates them and stamps each with the workflow's id.
    """

    id: str
    display_name: str
    tools: list[ToolSpec] = field(default_factory=list)
    config_defaults: dict = field(default_factory=dict)
    config_schema: dict | None = None
    produces_artifacts: bool = False
    subscriptions: list[Subscription] = field(default_factory=list)
    config_normalizer: Callable[[Any], dict] | None = None


@dataclass(frozen=True)
class Subscription:
    """A workflow's binding into one pipeline hook slot.

    ``priority`` only matters for fan-out slots (``PRE_PIPELINE``, ``POST_PIPELINE``); single-dispatch slots are resolved by
    workflow id and ignore it.
    """

    hook_type: HookType
    callable: Callable
    priority: int = 0
    workflow_id: str = ""


class ToolNameCollision(Exception):
    """Raised when a workflow tries to claim a tool name that is already
    taken by a built-in pass or by another registered workflow."""


class WorkflowMandateError(ValueError):
    """Raised by ``finalize_registry`` when a ``produces_artifacts=True`` workflow lacks ``REGENERATE`` and/or ``REROLL_GEN``.
    Failing at import rather than on the first regen click avoids shipping a half-bound artifact workflow into production.
    """


class WorkflowDeclarationError(ValueError):
    """Raised when a workflow's static declaration is internally invalid."""


_WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ARTIFACT_HOOKS = frozenset({HookType.REGENERATE, HookType.REROLL_GEN, HookType.EXPORT})

# The name a plug-in package exports its ``Workflow`` under.
PLUGIN_EXPORT = "WORKFLOW"


_WORKFLOWS_BY_ID: dict[str, Workflow] = {}


def _declared_function_name(payload: object) -> object:
    if not isinstance(payload, dict):
        return None
    function = payload.get("function")
    return function.get("name") if isinstance(function, dict) else None


def _check_hook(w: Workflow, hook_type: HookType, bound: Collection[HookType]) -> None:
    """Reject a second binding of one slot, or an artifact slot on a workflow without artifacts."""
    if hook_type in bound:
        raise WorkflowDeclarationError(f"workflow {w.id!r} already has a {hook_type.value} subscription")
    if hook_type in _ARTIFACT_HOOKS and not w.produces_artifacts:
        raise WorkflowDeclarationError(
            f"workflow {w.id!r} cannot subscribe to {hook_type.value} without produces_artifacts=True"
        )


def _declared_subscriptions(w: Workflow) -> list[Subscription]:
    """Validate the subscriptions *w* declares and stamp each with its id."""
    stamped: list[Subscription] = []
    for sub in w.subscriptions:
        if not isinstance(sub, Subscription) or not isinstance(sub.hook_type, HookType) or not callable(sub.callable):
            raise WorkflowDeclarationError(
                f"workflow {w.id!r} subscriptions must be Subscription(HookType, callable, priority) records"
            )
        if sub.workflow_id not in ("", w.id):
            raise WorkflowDeclarationError(f"workflow {w.id!r} declares a subscription for workflow {sub.workflow_id!r}")
        _check_hook(w, sub.hook_type, [s.hook_type for s in stamped])
        stamped.append(replace(sub, workflow_id=w.id))
    return stamped


def register_workflow(w: Workflow) -> None:
    """Register or replace a workflow, with the subscriptions it declares."""
    if not isinstance(w.id, str) or _WORKFLOW_ID_RE.fullmatch(w.id) is None:
        raise WorkflowDeclarationError(
            f"workflow id {w.id!r} must be 1-64 ASCII letters, digits, underscores, or hyphens and start with a letter or digit"
        )
    subscriptions = _declared_subscriptions(w)

    seen_tool_names: set[str] = set()
    for spec in w.tools:
        if not isinstance(spec.name, str) or _TOOL_NAME_RE.fullmatch(spec.name) is None:
            raise WorkflowDeclarationError(
                f"workflow {w.id!r} tool name {spec.name!r} must be 1-64 ASCII letters, digits, underscores, or hyphens"
            )
        if spec.name in seen_tool_names:
            raise WorkflowDeclarationError(f"workflow {w.id!r} declares duplicate tool name {spec.name!r}")
        seen_tool_names.add(spec.name)

        schema_name = _declared_function_name(spec.schema)
        if schema_name != spec.name:
            raise WorkflowDeclarationError(
                f"workflow {w.id!r} tool {spec.name!r} schema function name must match the declared name"
            )
        choice_name = _declared_function_name(spec.choice)
        if choice_name != spec.name:
            raise WorkflowDeclarationError(
                f"workflow {w.id!r} tool {spec.name!r} choice function name must match the declared name"
            )

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
        remove_tool(orphan)

    w.subscriptions[:] = subscriptions
    _WORKFLOWS_BY_ID[w.id] = w


def register_plugins(package: str, directory: Path) -> None:
    """Import and register every workflow plug-in package in *directory*, in package-name order.

    A plug-in is a subdirectory with an ``__init__.py`` that exports
    ``WORKFLOW``: a ``Workflow`` whose id is the package name, carrying its
    hook subscriptions. Modules directly in *directory* are host modules, not
    plug-ins. An import error propagates, and a directory of Python modules
    without an ``__init__.py`` or a package without a valid ``WORKFLOW`` raises
    ``WorkflowDeclarationError``, so a broken plug-in stops startup instead of
    going missing.
    """
    for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if not path.is_dir() or path.name == "__pycache__":
            continue
        if not (path / "__init__.py").is_file():
            if any(path.rglob("*.py")):
                raise WorkflowDeclarationError(
                    f"{path} holds Python modules but no __init__.py; a workflow plug-in must be a package"
                )
            continue
        module = importlib.import_module(f"{package}.{path.name}")
        workflow = getattr(module, PLUGIN_EXPORT, None)
        if not isinstance(workflow, Workflow):
            raise WorkflowDeclarationError(f"workflow plug-in {module.__name__} must export {PLUGIN_EXPORT} as a Workflow")
        if workflow.id != path.name:
            raise WorkflowDeclarationError(
                f"workflow plug-in {module.__name__} declares id {workflow.id!r}; its id must be its package name"
            )
        register_workflow(workflow)


# One overload per slot, so the type checker holds each hook to the shape its route or the bridge calls it with.
@overload
def subscription(hook_type: Literal[HookType.PRE_PIPELINE], fn: PreHook, *, priority: int = 0) -> Subscription: ...
@overload
def subscription(hook_type: Literal[HookType.POST_PIPELINE], fn: PostHook, *, priority: int = 0) -> Subscription: ...
@overload
def subscription(hook_type: Literal[HookType.ON_DEMAND], fn: OnDemandHook, *, priority: int = 0) -> Subscription: ...
@overload
def subscription(hook_type: Literal[HookType.REGENERATE], fn: RegenHook, *, priority: int = 0) -> Subscription: ...
@overload
def subscription(hook_type: Literal[HookType.REROLL_GEN], fn: RerollGenHook, *, priority: int = 0) -> Subscription: ...
@overload
def subscription(hook_type: Literal[HookType.QUERY], fn: QueryHook, *, priority: int = 0) -> Subscription: ...
@overload
def subscription(hook_type: Literal[HookType.UPLOAD], fn: UploadHook, *, priority: int = 0) -> Subscription: ...
@overload
def subscription(hook_type: Literal[HookType.EXPORT], fn: ExportHook, *, priority: int = 0) -> Subscription: ...
def subscription(hook_type: HookType, fn: Callable, *, priority: int = 0) -> Subscription:
    """A hook binding for a plug-in's ``Workflow.subscriptions``, typed per slot."""
    return Subscription(hook_type, fn, priority)


# The same per-slot overloads, for a hook a host adapter binds to a registered plug-in.
@overload
def subscribe(workflow_id: str, hook_type: Literal[HookType.PRE_PIPELINE], fn: PreHook, *, priority: int = 0) -> None: ...
@overload
def subscribe(workflow_id: str, hook_type: Literal[HookType.POST_PIPELINE], fn: PostHook, *, priority: int = 0) -> None: ...
@overload
def subscribe(workflow_id: str, hook_type: Literal[HookType.ON_DEMAND], fn: OnDemandHook, *, priority: int = 0) -> None: ...
@overload
def subscribe(workflow_id: str, hook_type: Literal[HookType.REGENERATE], fn: RegenHook, *, priority: int = 0) -> None: ...
@overload
def subscribe(workflow_id: str, hook_type: Literal[HookType.REROLL_GEN], fn: RerollGenHook, *, priority: int = 0) -> None: ...
@overload
def subscribe(workflow_id: str, hook_type: Literal[HookType.QUERY], fn: QueryHook, *, priority: int = 0) -> None: ...
@overload
def subscribe(workflow_id: str, hook_type: Literal[HookType.UPLOAD], fn: UploadHook, *, priority: int = 0) -> None: ...
@overload
def subscribe(workflow_id: str, hook_type: Literal[HookType.EXPORT], fn: ExportHook, *, priority: int = 0) -> None: ...
def subscribe(workflow_id: str, hook_type: HookType, fn: Callable, *, priority: int = 0) -> None:
    """Bind *fn* to a registered workflow's hook slot.

    Plug-ins declare their hooks in ``Workflow.subscriptions``; this binds a hook a host adapter supplies for a plug-in.
    """
    record = _WORKFLOWS_BY_ID.get(workflow_id)
    if record is None:
        raise LookupError(f"subscribe: workflow {workflow_id!r} not registered")
    _check_hook(record, hook_type, [s.hook_type for s in record.subscriptions])
    record.subscriptions.append(Subscription(hook_type, fn, priority, workflow_id))


def iter_subscriptions(hook_type: HookType) -> list[Subscription]:
    """Return subscriptions of ``hook_type`` sorted by priority ascending.

    Tie-break is registration order: ``dict`` insertion order plus
    Python's stable sort preserves it without an explicit secondary key.
    """
    subs = [s for w in _WORKFLOWS_BY_ID.values() for s in w.subscriptions if s.hook_type is hook_type]
    subs.sort(key=lambda s: s.priority)
    return subs


def get_subscription(workflow_id: str, hook_type: HookType) -> Subscription | None:
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
    """Validate that every ``produces_artifacts=True`` workflow has both ``REGENERATE`` and ``REROLL_GEN`` subscriptions.

    Invoke once every plug-in is registered and every host-adapter hook is bound -- this is the only hook that fails import on
    a partially-bound artifact workflow rather than deferring the crash to the first regen click.
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


def get_workflow(workflow_id: str) -> Workflow | None:
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
    """Read global config, falling back to a fresh defaults copy or {} if unregistered.

    Hold workflow_config_lock across any read-modify-write using this result.
    """
    raw = await _db_get_workflow_config(workflow_id)
    if raw:
        return raw
    w = _WORKFLOWS_BY_ID.get(workflow_id)
    if w is not None:
        return deepcopy(w.config_defaults)
    return {}


async def set_workflow_config(workflow_id: str, payload: dict) -> None:
    """Write the workflow's global config slot. Empty dict clears it.

    Caller must hold ``workflow_config_lock()`` across the read-then-write the payload was computed from. Direct use without the
    lock is safe for blind-replace writes; RMW sequences (``get_workflow_config`` -> mutate -> ``set_workflow_config``) silently
    lose writes under contention because the read happens in a separate transaction outside the lock.
    """
    await _db_set_workflow_config(workflow_id, payload)


def overlay_enable_tools(base: Mapping[str, bool], contribution: set[str] | Mapping[str, bool] | None) -> dict[str, bool]:
    """Return a mutable base copy with enabled contributions merged; false is ignored.

    Accept a set or Mapping contribution and any Mapping base. Validation/warnings belong to the caller.
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
