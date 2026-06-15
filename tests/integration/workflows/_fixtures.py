"""Test helpers for workflow hook coverage and workflow_attachments rows.

``register_for_test`` snapshots ``_registry._WORKFLOWS_BY_ID``, ``TOOLS``,
and ``STANDALONE_TOOLS`` with ``deepcopy`` on enter and restores them on
exit, so a failed assertion inside the ``with`` block cannot leak
registry mutations into adjacent tests. The same ``Workflow`` instance is
held by both the test and the registry (see clear at end of
``register_for_test``).
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator

import pytest

from backend.database.queries.conversations import (
    get_workflow_state,
    set_workflow_state,
)
from backend.database.queries.workflow_attachments import get_workflow_attachment_by_id
from backend.tool_registry import STANDALONE_TOOLS, TOOLS
from backend.workflows import (
    HookType,
    ToolSpec,
    Workflow,
    finalize_registry,
    register_workflow,
    subscribe,
)
from backend.workflows import registry as _registry


async def must_get_workflow_attachment(att_id: int) -> dict:
    """Fetch a workflow_attachments row and assert it exists.

    Use only when the row's existence is guaranteed by test setup
    (just-seeded or freshly-inserted). Tests that exercise the "row
    is absent" path must call ``get_workflow_attachment_by_id``
    directly and check for ``None``.
    """
    row = await get_workflow_attachment_by_id(att_id)
    assert row is not None, f"workflow_attachment {att_id} should exist after seeding"
    return row


@pytest.fixture(autouse=True)
def _restore_registry():
    """Snapshot the global workflow registry and tool tables, restore on exit.

    Tests in these modules call ``register_workflow`` / ``set_workflow_config``
    directly (not through ``register_for_test``'s ``with`` block), so without
    this autouse guard their registrations leak into adjacent tests. Imported
    by name into each such module -- pytest honours an imported fixture's
    ``autouse`` flag within the importing module's scope, so the import alone
    activates it.
    """
    by_id_snapshot = {k: deepcopy(v) for k, v in _registry._WORKFLOWS_BY_ID.items()}
    tools_snapshot = {n: dict(spec) for n, spec in TOOLS.items()}
    standalone_snapshot = set(STANDALONE_TOOLS)
    yield
    _registry._WORKFLOWS_BY_ID.clear()
    _registry._WORKFLOWS_BY_ID.update(by_id_snapshot)
    TOOLS.clear()
    TOOLS.update(tools_snapshot)
    STANDALONE_TOOLS.clear()
    STANDALONE_TOOLS.update(standalone_snapshot)


def make_workflow(
    workflow_id: str,
    *,
    display_name: str | None = None,
    priority: int = 0,
    pre_pipeline=None,
    post_pipeline=None,
    on_demand=None,
    regenerate=None,
    reroll_gen=None,
    tools: list[ToolSpec] | None = None,
    config_defaults: dict | None = None,
    config_schema: dict | None = None,
    produces_artifacts: bool = False,
) -> Workflow:
    """Construct a ``Workflow`` with test-friendly defaults.

    Hook kwargs are staged onto a ``_pending_hooks`` attribute that
    ``register_for_test`` consumes via ``subscribe`` after registration --
    direct construction of ``Workflow`` cannot bind subscriptions because
    that requires the record to be in the registry first.
    """
    meta = Workflow(
        id=workflow_id,
        display_name=display_name or f"Test workflow {workflow_id}",
        tools=tools or [],
        config_defaults=config_defaults or {},
        config_schema=config_schema,
        produces_artifacts=produces_artifacts,
    )
    pending: list[tuple[HookType, object, int]] = []
    for fn, hook_type in (
        (pre_pipeline, HookType.PRE_PIPELINE),
        (post_pipeline, HookType.POST_PIPELINE),
        (on_demand, HookType.ON_DEMAND),
        (regenerate, HookType.REGENERATE),
        (reroll_gen, HookType.REROLL_GEN),
    ):
        if fn is not None:
            pending.append((hook_type, fn, priority))
    meta._pending_hooks = pending  # type: ignore[attr-defined]
    return meta


@contextmanager
def register_for_test(workflow: Workflow, *, finalize: bool = True) -> Iterator[Workflow]:
    """Register *workflow* for the duration of a ``with`` block.

    On enter: registers the workflow and applies each pending subscription
    staged by ``make_workflow``. When ``finalize`` is True (the default),
    runs ``finalize_registry()`` after all subscriptions are bound so any
    ``produces_artifacts=True`` workflow missing its ``REGENERATE`` /
    ``REROLL_GEN`` subscriptions raises before the test body runs. Tests
    that exercise the mandate's raise path pass ``finalize=False`` to skip
    the validation.

    On exit: restores the registry, ``TOOLS``, and ``STANDALONE_TOOLS`` to
    a deep-copied snapshot captured before enter so subscription mutations
    inside the block cannot leak across teardown.
    """
    by_id_snapshot = {k: deepcopy(v) for k, v in _registry._WORKFLOWS_BY_ID.items()}
    tools_snapshot = {n: dict(spec) for n, spec in TOOLS.items()}
    standalone_snapshot = set(STANDALONE_TOOLS)

    register_workflow(workflow)
    for hook_type, fn, priority in getattr(workflow, "_pending_hooks", []):
        subscribe(workflow.id, hook_type, fn, priority=priority)
    if finalize:
        finalize_registry()
    try:
        yield workflow
    finally:
        _registry._WORKFLOWS_BY_ID.clear()
        _registry._WORKFLOWS_BY_ID.update(by_id_snapshot)
        TOOLS.clear()
        TOOLS.update(tools_snapshot)
        STANDALONE_TOOLS.clear()
        STANDALONE_TOOLS.update(standalone_snapshot)
        # register_workflow stores the same Workflow instance the test holds,
        # so workflow.subscriptions is identity-shared with the registry's
        # record. Restoring the dict to the deepcopied snapshot above does
        # not touch the original list. Clearing it here lets the same
        # Workflow object be re-used in a subsequent register_for_test block
        # without subscribe() tripping its duplicate-(workflow_id, hook_type)
        # guard.
        workflow.subscriptions.clear()


# Each factory below returns ``(hook, gate, release)``: the hook awaits
# ``gate`` before its body and sets ``release`` once past it, so tests can
# both block a hook mid-execution and observe when it has actually
# entered. Per-hook signatures match the kind's contract in
# backend/workflows/contracts.py: pre/post are async generators
# taking ``(ctx)``, on_demand and regenerate are coroutines taking
# ``(ctx, body)``, reroll_gen takes ``(ctx, params, seed)``.


def _gated_async_generator(gate: asyncio.Event, release: asyncio.Event):
    async def hook(_ctx):
        await gate.wait()
        release.set()
        if False:
            yield  # pragma: no cover -- async generator with no yields

    return hook


def gated_pre_pipeline_hook() -> tuple[Any, asyncio.Event, asyncio.Event]:
    gate, release = asyncio.Event(), asyncio.Event()
    return _gated_async_generator(gate, release), gate, release


def gated_post_pipeline_hook() -> tuple[Any, asyncio.Event, asyncio.Event]:
    gate, release = asyncio.Event(), asyncio.Event()
    return _gated_async_generator(gate, release), gate, release


def gated_on_demand_hook() -> tuple[Any, asyncio.Event, asyncio.Event]:
    gate, release = asyncio.Event(), asyncio.Event()

    async def hook(_ctx, _body):
        await gate.wait()
        release.set()
        return {}

    return hook, gate, release


def gated_regen_hook() -> tuple[Any, asyncio.Event, asyncio.Event]:
    gate, release = asyncio.Event(), asyncio.Event()

    async def hook(_ctx, _body):
        await gate.wait()
        release.set()
        return []

    return hook, gate, release


def gated_reroll_gen_hook(bytes_to_return: bytes) -> tuple[Any, asyncio.Event, asyncio.Event]:
    gate, release = asyncio.Event(), asyncio.Event()

    async def hook(_ctx, _params, _seed):
        await gate.wait()
        release.set()
        return bytes_to_return

    return hook, gate, release


def counter_on_demand_hook(wid: str, key: str):
    """Returns an on_demand callable that does RMW counter increment on the
    conversation's workflow_state slot. Caller serialization is expected to
    come from ``api_trigger_workflow`` holding ``workflow_state_lock``.
    """

    async def hook(ctx, _body):
        state = await get_workflow_state(ctx.conversation_id, wid) or {}
        state[key] = int(state.get(key, 0)) + 1
        await set_workflow_state(ctx.conversation_id, wid, state)
        return {}

    return hook


def counter_post_pipeline_hook(wid: str, key: str):
    """Returns a post_pipeline async-generator hook that does RMW counter
    increment on the conversation's workflow_state slot. Caller
    serialization is expected to come from the orchestrator's per-iteration
    ``workflow_state_lock`` acquisition.
    """

    async def hook(ctx):
        state = await get_workflow_state(ctx.conversation_id, wid) or {}
        state[key] = int(state.get(key, 0)) + 1
        await set_workflow_state(ctx.conversation_id, wid, state)
        if False:
            yield  # pragma: no cover -- async generator with no yields

    return hook


class CallRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def append(self, label: str, arg: Any = None) -> None:
        self.calls.append((label, arg))

    def count(self, label: str) -> int:
        return sum(1 for c in self.calls if c[0] == label)
