"""Unit coverage for the attachment-group lock stabilization.

The group root id (``parent_attachment_id or id``) is a *mutable* identity:
deleting a root promotes a surviving sibling to a new root. ``locked_attachment_group``
must resolve the root, lock it, then RE-READ under the lock and retry on the
promoted root if the identity moved while acquiring -- otherwise a caller mutates
the group under a stale lock key or feeds a generative hook a since-deleted parent.

These drive the context manager directly with a scripted ``get_workflow_attachment_by_id``
so the retry is deterministic -- no sleeps, no real concurrency.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import backend.api.deps as deps


def _row(aid: int, *, parent: int | None, mid: int = 9) -> dict:
    return {"id": aid, "message_id": mid, "parent_attachment_id": parent}


def _scripted(monkeypatch, snapshots: list) -> list[int]:
    reads: list[int] = []

    async def fake_get(aid):
        reads.append(aid)
        return snapshots.pop(0)

    monkeypatch.setattr(deps, "get_workflow_attachment_by_id", fake_get)
    return reads


@pytest.mark.asyncio
async def test_retries_onto_the_promoted_root(monkeypatch):
    # aid=2 is a sibling of root 1. Between the pre-lock snapshot (still under root
    # 1) and the in-lock re-read, a concurrent delete promotes 2 to root (parent ->
    # None). The loop must drop the stale lock on 1 and re-resolve on 2.
    snapshots = [
        _row(2, parent=1),  # before: stale, still under root 1 -> lock(1)
        _row(2, parent=None),  # current under lock(1): promoted -> retry
        _row(2, parent=None),  # before (retry) -> lock(2)
        _row(2, parent=None),  # current under lock(2): stable -> yield
    ]
    _scripted(monkeypatch, snapshots)

    async with deps.locked_attachment_group(2, 9) as (att, root_id):
        assert root_id == 2, "must stabilize on the promoted root, not the stale one"
        assert att["parent_attachment_id"] is None

    assert snapshots == [], "two attempts consumed all four scripted reads"


@pytest.mark.asyncio
async def test_stable_group_yields_on_first_attempt(monkeypatch):
    snapshots = [_row(2, parent=1), _row(2, parent=1)]
    _scripted(monkeypatch, snapshots)

    async with deps.locked_attachment_group(2, 9) as (att, root_id):
        assert root_id == 1

    assert snapshots == [], "exactly two reads (snapshot + in-lock recheck)"


@pytest.mark.asyncio
async def test_target_that_is_its_own_root(monkeypatch):
    snapshots = [_row(5, parent=None), _row(5, parent=None)]
    _scripted(monkeypatch, snapshots)

    async with deps.locked_attachment_group(5, 9) as (_att, root_id):
        assert root_id == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [None, {"id": 2, "message_id": 999, "parent_attachment_id": None}],
    ids=["missing", "off-message"],
)
async def test_404_when_target_missing_or_off_message(monkeypatch, row):
    async def fake_get(aid):
        return row

    monkeypatch.setattr(deps, "get_workflow_attachment_by_id", fake_get)

    with pytest.raises(HTTPException) as exc:
        async with deps.locked_attachment_group(2, 9):
            pass
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_404_when_target_vanishes_under_the_lock(monkeypatch):
    # Exists at snapshot, gone by the in-lock re-read (deleted while acquiring).
    snapshots = [_row(2, parent=1), None]
    _scripted(monkeypatch, snapshots)

    with pytest.raises(HTTPException) as exc:
        async with deps.locked_attachment_group(2, 9):
            pass
    assert exc.value.status_code == 404
