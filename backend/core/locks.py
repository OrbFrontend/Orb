"""Process-level asyncio locks crossed by more than one backend module.

Locks confined to a single module (workflow-attachment root, conversation
stream) live alongside that module; this file holds only the locks that
are taken from both ``backend.main`` routes and the pipeline iteration in
``backend.orchestrator``, so they need a neutral home to avoid a circular
import.

``workflow_state_lock(cid, workflow_id)`` is acquired around each
pre/post pipeline hook callable's full lifetime, including the
``async for`` yielding SSE events out of a post-pipeline hook. A
concurrent ``/trigger`` on the same ``(cid, workflow_id)`` therefore
waits for the in-flight stream to drain, not just for hook compute --
this is deliberate, so the hook's ``workflow_state`` read-then-write
window cannot be clobbered mid-stream. ``asyncio.Lock`` is non-reentrant,
so a hook callable must not re-enter an HTTP route that takes this lock
for the same pair.

``workflow_character_state_lock(character_id, workflow_id)`` is the
per-character analogue, keyed by ``character_id`` rather than ``cid``
because one character card is referenced by many conversations
(``conversations.character_card_id`` is nullable and non-FK), so two
concurrent turns on different conversations of the same character would
otherwise read-then-write the same ``character_cards.workflow_state``
slot under different keys and clobber each other. It is acquired INSIDE
``workflow_state_lock`` at every site (conversation lock outer, character
lock inner) to fix one global acquisition order; the same non-reentrancy
rule applies.

``workflow_config_lock()`` serializes ALL ``workflow_config``
read-then-write windows across the process regardless of workflow id,
because every slot lives in one JSON blob on the global ``settings``
row, so the read pulls from a single shared cell. Callers doing RMW
against ``workflow_config[$.<wid>]`` must hold this lock from before
``get_workflow_config`` until after ``set_workflow_config``. A single
``set_workflow_config`` call with no prior read is safe at the SQL
layer without the lock.

``workflow_config_lock`` keys its dict by running event loop because
``asyncio.Lock`` binds to its loop on first acquire and pytest-asyncio
gives each test a fresh function-scope loop; without per-loop keying,
the second test would acquire a Lock bound to a dead loop and crash.
"""

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


_maintenance_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


@asynccontextmanager
async def maintenance_lock():
    """Serialize whole-database maintenance: preset export/import/apply,
    snapshot create, and full-file restore.

    These operations read the entire DB file (``VACUUM INTO``) or swap it on
    disk, so two running concurrently -- or one running while another is
    mid-swap -- would observe or produce a torn database. Keyed by running
    event loop for the same reason as ``workflow_config_lock`` (a ``Lock``
    binds to the loop of its first acquire; pytest-asyncio hands each test a
    fresh loop).

    Routine CRUD routes do NOT take this lock. On this single-user local app
    these maintenance actions are deliberate, user-initiated, and not issued
    concurrently with active generation, so gating only the maintenance side
    is sufficient.
    """
    loop = asyncio.get_running_loop()
    lock = _maintenance_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        yield
