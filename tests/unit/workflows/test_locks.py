"""Shared behavioral contract for workflow-scoped locks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import pytest

from backend.core.locks import workflow_character_state_lock, workflow_state_lock

LockFactory = Callable[[str, str], AbstractAsyncContextManager[None]]
lock_cases = pytest.mark.parametrize(
    "lock",
    [workflow_state_lock, workflow_character_state_lock],
    ids=["conversation-state", "character-state"],
)


async def _hold(lock: LockFactory, scope: str, wid: str, gate: asyncio.Event, release: asyncio.Event) -> None:
    async with lock(scope, wid):
        gate.set()
        await release.wait()


@lock_cases
async def test_same_pair_serializes(lock: LockFactory):
    first_in = asyncio.Event()
    first_can_exit = asyncio.Event()
    second_in = asyncio.Event()
    second_can_exit = asyncio.Event()

    first = asyncio.create_task(_hold(lock, "scope", "workflow", first_in, first_can_exit))
    await first_in.wait()

    second = asyncio.create_task(_hold(lock, "scope", "workflow", second_in, second_can_exit))
    await asyncio.sleep(0.05)
    assert not second_in.is_set(), "second acquirer entered while first held the lock"

    first_can_exit.set()
    await first
    await second_in.wait()
    second_can_exit.set()
    await second


@lock_cases
@pytest.mark.parametrize(
    ("left", "right"),
    [
        (("scope", "workflow-a"), ("scope", "workflow-b")),
        (("scope-a", "workflow"), ("scope-b", "workflow")),
    ],
    ids=["different-workflows", "different-scopes"],
)
async def test_different_keys_do_not_serialize(lock: LockFactory, left: tuple[str, str], right: tuple[str, str]):
    in_a = asyncio.Event()
    in_b = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()

    task_a = asyncio.create_task(_hold(lock, *left, in_a, release_a))
    task_b = asyncio.create_task(_hold(lock, *right, in_b, release_b))

    await asyncio.wait_for(asyncio.gather(in_a.wait(), in_b.wait()), timeout=1.0)

    release_a.set()
    release_b.set()
    await asyncio.gather(task_a, task_b)
