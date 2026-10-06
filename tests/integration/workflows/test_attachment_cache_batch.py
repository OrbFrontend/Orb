"""Integration tests for ``insert_workflow_attachments`` (batch path).

The batch entry point is what ``add_message`` uses to land workflow artifacts atomically with the parent message INSERT.
"""

import json

import pytest

from backend.database import add_message, get_workflow_attachments_for_message, insert_workflow_attachment_row, set_active_leaf
from backend.database.connection import get_db
from backend.workflows.attachment_cache import (
    EVICTED_MARKER,
    OVERSIZE_NO_METADATA_REASON,
    WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON,
    insert_workflow_attachments,
)

from ._fixtures import must_get_workflow_attachment, registered_artifact_workflow
from ._fixtures import new_conversation as _new_conversation
from ._fixtures import seed_message as _seed_message


@pytest.fixture(autouse=True)
def _register_wf_workflow():
    """Register the ``"wf"`` workflow with produces_artifacts=True for every test in this module. Batch helper gates on
    producer-workflow registration; without this fixture every entry would land in the policy-rejection partition instead of
    exercising the oversize / eviction branches under test.
    """
    with registered_artifact_workflow():
        yield


async def _seed_row(
    mid: int, *, wid: str = "wf", data: bytes = b"X", recent: list[int] | None = None, recoverable: bool = True
) -> int:
    att = {"filename": "x", "mime": "application/octet-stream", "data": data, "workflow_id": wid}
    if recoverable:
        att.update({"seed": f"seed-{wid}", "generation_metadata": {}})
    rid = await insert_workflow_attachment_row(mid, att)
    if recent is not None:
        async with get_db() as db:
            await db.execute("UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?", (json.dumps(recent), rid))
            await db.commit()
    return rid


async def _set_budget(db, bytes_limit: int) -> None:
    await db.execute("UPDATE settings SET attachment_cache_budget_bytes = ? WHERE id = 1", (bytes_limit,))
    await db.commit()


def _make_att(name: str, data: bytes, *, wid: str = "wf", parent: int | None = None, seed: str | None = "seed") -> dict:
    a: dict = {"filename": name, "mime": "image/png", "data": data, "workflow_id": wid}
    if parent is not None:
        a["parent_attachment_id"] = parent
    if seed is not None:
        a.update(seed=seed, generation_metadata={})
    return a


async def _evicted(rids) -> list[bool]:
    return [(await must_get_workflow_attachment(rid))["data_b64"] == EVICTED_MARKER for rid in rids]


async def test_empty_batch_returns_empty_list(client):
    _, mid = await _seed_message(client)
    assert await insert_workflow_attachments(mid, []) == ([], [])


async def test_whole_batch_birth_as_access_for_every_row(client, db):
    _, mid = await _seed_message(client)
    await _set_budget(db, 1000)
    before = (await db.one("SELECT attachment_access_counter AS c FROM settings WHERE id = 1"))["c"]
    new_ids, _ = await insert_workflow_attachments(mid, [_make_att(f"a{i}", b"X" * 5) for i in range(4)])
    for rid in new_ids:
        row = await must_get_workflow_attachment(rid)
        assert len(json.loads(row["recent_accesses"])) == 1, "every new row gets one birth access entry"
    assert (await db.one("SELECT attachment_access_counter AS c FROM settings WHERE id = 1"))["c"] - before == 4
    # SQLite AUTOINCREMENT yields strictly increasing ids in insert order.
    assert new_ids == sorted(new_ids)


@pytest.mark.parametrize(
    "existing,budget,new,existing_evicted,new_evicted",
    [
        ([], 1000, [10, 20, 30], [], [False, False, False]),  # the whole batch fits
        # Existing 30 + new 20 against 40: evict the oldest existing row only.
        ([(10, 1), (10, 2), (10, 999)], 40, [10, 10], [True, False, False], [False, False]),
        # Existing 50 + new 30 against 50: evict exactly the three oldest.
        ([(10, age) for age in (1, 2, 3, 4, 999)], 50, [10, 10, 10], [True, True, True, False, False], [False] * 3),
        # Step A markers the biggest new rows until the batch fits beside existing: big+mid marker, existing preserved.
        ([(10, 1)], 15, [50, 30, 5], [False], [True, True, False]),
        ([], 5, [100], [], [True]),  # a marker row stores the sentinel
        ([(5, 1)], 20, [100, 80], [False], [True, True]),  # a hopeless batch markers every new row
        # Runtime over budget (budget shrunk under existing rows): the next write evicts existing to converge.
        ([(4, 10)], 1, [100], [True], [True]),
    ],
)
async def test_batch_eviction_and_marker_policy(client, db, existing, budget, new, existing_evicted, new_evicted):
    _, mid = await _seed_message(client)
    seeded = [await _seed_row(mid, data=b"E" * size, recent=[age]) for size, age in existing]
    await _set_budget(db, budget)
    new_ids, _ = await insert_workflow_attachments(
        mid, [_make_att(f"n{i}", b"N" * size, seed=f"s{i}") for i, size in enumerate(new)]
    )
    assert await _evicted(seeded) == existing_evicted
    assert await _evicted(new_ids) == new_evicted


async def test_marker_birth_as_access_still_records(client, db):
    _, mid = await _seed_message(client)
    await _set_budget(db, 1)  # any non-trivial att triggers marker
    [rid], _ = await insert_workflow_attachments(mid, [_make_att("huge", b"H" * 100)])
    row = await must_get_workflow_attachment(rid)
    assert row["data_b64"] == EVICTED_MARKER
    assert len(json.loads(row["recent_accesses"])) == 1, "marker rows still get a birth access entry"


async def test_batch_never_evicts_existing_unrecoverable_bytes(client, db):
    _, mid = await _seed_message(client)
    pinned = await _seed_row(mid, data=b"PINNED", recent=[1], recoverable=False)
    evictable = await _seed_row(mid, data=b"OLD", recent=[2])
    await _set_budget(db, 7)
    new_ids, rejected = await insert_workflow_attachments(mid, [_make_att("new", b"NEW")])
    assert len(new_ids) == 1 and rejected == []
    assert await _evicted([pinned, evictable]) == [False, True]


@pytest.mark.parametrize("mark_active", [True, False])
async def test_mark_active_per_att_with_parent(client, mark_active):
    _, mid = await _seed_message(client)
    root = await _seed_row(mid)
    new_ids, _ = await insert_workflow_attachments(
        mid, [_make_att("s1", b"S1", parent=root), _make_att("s2", b"S2", parent=root)], mark_active=mark_active
    )
    # The last sibling of the same root wins.
    assert (await must_get_workflow_attachment(root))["active_sibling_id"] == (new_ids[-1] if mark_active else None)


async def test_root_inserts_dont_touch_active_pointer(client):
    _, mid = await _seed_message(client)
    new_ids, _ = await insert_workflow_attachments(mid, [_make_att("r1", b"R1"), _make_att("r2", b"R2")])
    for rid in new_ids:
        assert (await must_get_workflow_attachment(rid))["active_sibling_id"] is None


async def test_insert_workflow_attachments_rejects_foreign_message_parent(client):
    cid = await _new_conversation(client)
    mid_a, _ = await add_message(cid, "assistant", "scene A", 0)
    mid_b, _ = await add_message(cid, "assistant", "scene B", 1, parent_id=mid_a)
    await set_active_leaf(cid, mid_b)
    root_on_a = await _seed_row(mid_a)
    with pytest.raises(ValueError, match="belongs to message"):
        await insert_workflow_attachments(mid_b, [_make_att("sib", b"S", parent=root_on_a)])
    foreign_root = await must_get_workflow_attachment(root_on_a)
    assert foreign_root["active_sibling_id"] is None, "cross-message rejection must not write the foreign root's active pointer"


async def test_insert_workflow_attachments_db_branch_requires_active_transaction(client):
    _, mid = await _seed_message(client)
    async with get_db() as conn:
        assert conn.in_transaction is False
        with pytest.raises(RuntimeError, match="active write transaction"):
            await insert_workflow_attachments(mid, [_make_att("x", b"X")], db=conn)


async def test_caller_owned_tx_rolls_back_on_failure(client, db):
    _, mid = await _seed_message(client)
    existing = await _seed_row(mid, data=b"BEFORE")
    before = await must_get_workflow_attachment(existing)

    # A malformed att that passes the policy gate (a registered workflow_id) but trips the row helper's data/path check.
    with pytest.raises((ValueError, LookupError)):
        async with get_db() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            bad = {"filename": "bad", "mime": "image/png", "workflow_id": "wf"}  # no data / path
            await insert_workflow_attachments(mid, [_make_att("ok", b"OK"), bad], db=conn)
            await conn.commit()  # not reached

    assert await must_get_workflow_attachment(existing) == before
    rows = await get_workflow_attachments_for_message(mid)
    assert [r["id"] for r in rows] == [existing], "rollback discarded the partial batch"


@pytest.mark.parametrize("rehydratable", [False, True])
async def test_add_message_oversize_workflow_att(client, db, rehydratable):
    """An oversize att without seed + generation_metadata is dropped (and reported in add_message's rejected list) while the
    message still commits; a rehydratable one marker-inserts atomically with the message."""
    cid = await _new_conversation(client)
    user_mid, _ = await add_message(cid, "user", "u", 0)
    await set_active_leaf(cid, user_mid)
    await _set_budget(db, 5)
    huge = {"source": "workflow:wf", "workflow_id": "wf", "filename": "huge.png", "mime": "image/png", "data": b"H" * 100}
    if rehydratable:
        huge.update(seed="test-seed", generation_metadata={})

    asst_mid, rejected = await add_message(cid, "assistant", "draft", 0, parent_id=user_mid, attachments=[huge])

    assert len(await db.all("SELECT * FROM messages WHERE id = ?", (asst_mid,))) == 1
    wf_rows = await db.all("SELECT * FROM workflow_attachments WHERE message_id = ?", (asst_mid,))
    if rehydratable:
        assert [row["data_b64"] for row in wf_rows] == [EVICTED_MARKER]
        assert rejected == []
    else:
        assert wf_rows == []  # dropped: no row at all, not even a marker
        assert [(r["filename"], r["reason"]) for r in rejected] == [("huge.png", OVERSIZE_NO_METADATA_REASON)]


async def test_add_message_workflow_atts_skipped_when_message_insert_fails(client, db):
    """If the message INSERT raises (FK violation on parent_id), the workflow batch is never reached and nothing commits."""
    cid = await _new_conversation(client)
    before = (await db.one("SELECT COUNT(*) AS c FROM workflow_attachments"))["c"]
    huge = {"source": "workflow:wf", "workflow_id": "wf", "filename": "h.png", "mime": "image/png", "data": b"X"}
    with pytest.raises(ValueError, match="Foreign key constraint"):
        await add_message(cid, "assistant", "draft", 0, parent_id=99999, attachments=[huge])
    assert (await db.one("SELECT COUNT(*) AS c FROM workflow_attachments"))["c"] == before


async def test_batch_policy_gate_unregistered_workflow_rejected_without_eviction(client, db):
    """An unregistered workflow_id is moved to rejected_atts, and its size is excluded from byte accounting."""
    _, mid = await _seed_message(client)
    existing = await _seed_row(mid, data=b"KEEP", recent=[1])  # 4 bytes
    await _set_budget(db, 5)
    new_ids, rejected = await insert_workflow_attachments(
        mid, [_make_att("huge.bin", b"H" * 100, wid="stale"), _make_att("ok.bin", b"A", wid="wf")]
    )
    assert [(await must_get_workflow_attachment(rid))["workflow_id"] for rid in new_ids] == ["wf"]
    assert [(r["filename"], r["workflow_id"], r["reason"]) for r in rejected] == [
        ("huge.bin", "stale", WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON)
    ]
    assert await _evicted([existing]) == [False], "a policy-rejected entry must not count toward the byte budget"

    atts = [_make_att("a.bin", b"AAAA", wid="stale-a"), _make_att("b.bin", b"BB", wid="stale-b")]
    new_ids, rejected = await insert_workflow_attachments(mid, atts)
    assert new_ids == []
    assert {r["filename"] for r in rejected} == {"a.bin", "b.bin"}
    assert all(r["reason"] == WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON for r in rejected)
