"""Lifecycle coordination for executable community-extension invocations.

Registry snapshots deliberately let already-running work retain old compiled
objects. Destructive purge is the exception: it must prevent an old snapshot
from starting another invocation and wait until every invocation that already
started has left its commit boundary. This module is the small synchronization
surface shared by adapters, lifecycle mutations, and application shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class InvocationBlocked(RuntimeError):
    """The extension is disabled, removed, purging, or the app is stopping."""


_condition = asyncio.Condition()
_active: dict[str, set[asyncio.Task[Any]]] = {}
_blocked: set[str] = set()
_shutting_down = False


@asynccontextmanager
async def track_invocation(extension_id: str) -> AsyncIterator[None]:
    """Register one invocation unless lifecycle state currently blocks starts."""
    task = asyncio.current_task()
    if task is None:  # pragma: no cover - every production call runs in a task
        raise RuntimeError("extension invocation has no owning task")
    async with _condition:
        if _shutting_down or extension_id in _blocked:
            raise InvocationBlocked("the extension is not accepting new invocations")
        _active.setdefault(extension_id, set()).add(task)
    try:
        yield
    finally:
        async with _condition:
            tasks = _active.get(extension_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    _active.pop(extension_id, None)
            _condition.notify_all()


async def block_new_invocations(extension_id: str) -> None:
    """Prevent later starts, including callables retained by older snapshots."""
    async with _condition:
        _blocked.add(extension_id)


async def allow_new_invocations(extension_id: str) -> None:
    """Re-open an explicitly enabled or freshly installed extension."""
    async with _condition:
        if not _shutting_down:
            _blocked.discard(extension_id)
        _condition.notify_all()


async def drain_invocations(extension_id: str) -> None:
    """Wait until all invocations that entered before the block have finished."""
    async with _condition:
        await _condition.wait_for(lambda: not _active.get(extension_id))


async def reset_for_startup() -> None:
    """Reset process-local coordination before publishing the startup catalog."""
    global _shutting_down
    async with _condition:
        if _active:
            raise RuntimeError("cannot reset extension execution while invocations are active")
        _blocked.clear()
        _shutting_down = False
        _condition.notify_all()


async def cancel_and_drain_all() -> None:
    """Stop new work and cancel every owning task during application shutdown."""
    global _shutting_down
    current = asyncio.current_task()
    async with _condition:
        _shutting_down = True
        tasks = {task for active in _active.values() for task in active if task is not current}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    async with _condition:
        await _condition.wait_for(lambda: not _active)


async def active_invocation_count(extension_id: str) -> int:
    """Test/diagnostic projection; callers cannot mutate tracker state."""
    async with _condition:
        return len(_active.get(extension_id, ()))
