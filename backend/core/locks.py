"""Process-level asyncio locks shared across backend layers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

_workflow_state_locks: dict[tuple[str, str], asyncio.Lock] = {}


@asynccontextmanager
async def workflow_state_lock(cid: str, workflow_id: str):
    lock = _workflow_state_locks.setdefault((cid, workflow_id), asyncio.Lock())
    async with lock:
        yield


_workflow_character_state_locks: dict[tuple[str, str], asyncio.Lock] = {}


@asynccontextmanager
async def workflow_character_state_lock(character_id: str, workflow_id: str):
    lock = _workflow_character_state_locks.setdefault((character_id, workflow_id), asyncio.Lock())
    async with lock:
        yield


_workflow_config_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


@asynccontextmanager
async def workflow_config_lock():
    loop = asyncio.get_running_loop()
    lock = _workflow_config_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        yield


_world_apply_locks: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = {}


@asynccontextmanager
async def world_apply_lock(world_id: str):
    """Serialize mutations for one World."""
    loop = asyncio.get_running_loop()
    lock = _world_apply_locks.setdefault((loop, world_id), asyncio.Lock())
    async with lock:
        yield


_maintenance_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


@asynccontextmanager
async def maintenance_lock():
    """Serialize whole-database maintenance."""
    loop = asyncio.get_running_loop()
    lock = _maintenance_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        yield


_wal_anchor_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


@asynccontextmanager
async def wal_anchor_lock():
    """Serialize the WAL anchor's open/close.

    Opening awaits ``aiosqlite.connect``, which yields the loop between the
    "is one already open?" check and the assignment -- wide enough for two
    callers to both connect, the second to overwrite the global, and the first
    to be left unreachable. A leaked ``aiosqlite`` connection is not merely a
    stray file handle: its worker thread is non-daemon, so it keeps the process
    from exiting.
    """
    loop = asyncio.get_running_loop()
    lock = _wal_anchor_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        yield
