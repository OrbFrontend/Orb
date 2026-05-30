from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import backend.database.connection as db_connection
from backend.database import init_db
from backend.database.queries.settings import get_workflow_config, set_workflow_config
from backend.workflows.toolkit import workflow_config_lock


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    return tmp_path / "config_lock.db"


@pytest.fixture(autouse=True)
async def _init_db(db_path: Path, monkeypatch):
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    await init_db()


async def _rmw_increment_locked(wid: str, key: str) -> None:
    async with workflow_config_lock():
        state = await get_workflow_config(wid) or {}
        state[key] = int(state.get(key, 0)) + 1
        await set_workflow_config(wid, state)


async def test_concurrent_same_wid_no_lost_writes():
    wid = "wf_under_test"
    n = 20
    await asyncio.gather(*[_rmw_increment_locked(wid, "counter") for _ in range(n)])
    final = await get_workflow_config(wid)
    assert final == {"counter": n}


async def test_disjoint_wid_paths_compose_under_json_set():
    wids = [f"wf_{i}" for i in range(5)]
    await asyncio.gather(*[set_workflow_config(w, {"marker": w}) for w in wids])
    for w in wids:
        assert (await get_workflow_config(w)) == {"marker": w}


async def test_naked_rmw_without_lock_loses_writes():
    """All readers must complete before any writer proceeds.

    Without the gate, asyncio could interleave a read after another
    coroutine's write, hiding the lost-write race behind a non-deterministic
    schedule. The ``asyncio.Event`` forces every coroutine to read the same
    empty starting state, so the final counter is deterministically 1
    rather than something between 1 and ``n``.
    """
    wid = "wf_naked"
    n = 20
    all_reads_done = asyncio.Event()
    reads_completed = 0

    async def _racy_rmw() -> None:
        nonlocal reads_completed
        state = await get_workflow_config(wid) or {}
        reads_completed += 1
        if reads_completed == n:
            all_reads_done.set()
        await all_reads_done.wait()
        state["counter"] = int(state.get("counter", 0)) + 1
        await set_workflow_config(wid, state)

    await asyncio.gather(*[_racy_rmw() for _ in range(n)])
    final = await get_workflow_config(wid)
    assert final == {"counter": 1}, f"expected counter=1 (all readers saw same snapshot), got {final}"
