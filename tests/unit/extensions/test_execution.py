"""Invocation gating shared by disable, purge, and application shutdown."""

from __future__ import annotations

import asyncio

import pytest

from backend.features.extensions import execution


async def test_block_prevents_old_snapshots_starting_and_drain_waits_for_active_work():
    await execution.reset_for_startup()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def active() -> None:
        async with execution.track_invocation("scene-meter"):
            entered.set()
            await release.wait()

    worker = asyncio.create_task(active())
    await entered.wait()
    await execution.block_new_invocations("scene-meter")
    draining = asyncio.create_task(execution.drain_invocations("scene-meter"))
    await asyncio.sleep(0)
    assert not draining.done()

    with pytest.raises(execution.InvocationBlocked):
        async with execution.track_invocation("scene-meter"):
            pass

    release.set()
    await worker
    await draining
    await execution.allow_new_invocations("scene-meter")


async def test_shutdown_cancels_and_drains_active_invocation_tasks():
    await execution.reset_for_startup()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def active() -> None:
        try:
            async with execution.track_invocation("scene-meter"):
                entered.set()
                await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = asyncio.create_task(active())
    await entered.wait()
    await execution.cancel_and_drain_all()
    assert worker.cancelled()
    assert cancelled.is_set()
    await execution.reset_for_startup()
