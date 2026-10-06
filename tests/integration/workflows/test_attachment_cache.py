import base64
import json

import pytest

from backend.database import add_message, insert_workflow_attachment_row, set_active_leaf
from backend.workflows.attachment_cache import (
    EVICTED_MARKER,
    OVERSIZE_NO_METADATA_REASON,
    WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON,
    _get_budget_bytes_on,
    evict,
    insert_workflow_attachment,
    record_access,
    rehydrate_attachment,
    set_active_sibling,
)

from ._fixtures import must_get_workflow_attachment, registered_artifact_workflow
from ._fixtures import new_conversation as _new_conversation
from ._fixtures import seed_message as _seed_message


@pytest.fixture(autouse=True)
def _register_wf_workflow():
    """Register the ``"wf"`` workflow with produces_artifacts=True for every test in this module. The cache helpers gate on
    producer-workflow registration; without this fixture every helper call would short-circuit to the policy-rejection path
    and the oversize/eviction behaviors under test would never run.
    """
    with registered_artifact_workflow():
        yield


async def _seed_row(
    mid: int, *, wid: str = "wf", data: bytes = b"X", parent: int | None = None, recoverable: bool = True
) -> int:
    att = {"filename": "x", "mime": "application/octet-stream", "data": data, "workflow_id": wid}
    if recoverable:
        att.update({"seed": f"seed-{wid}", "generation_metadata": {}})
    if parent is not None:
        att["parent_attachment_id"] = parent
    return await insert_workflow_attachment_row(mid, att)


async def _insert(mid: int, data: bytes = b"S", **extra) -> tuple[int | None, dict | None]:
    return await insert_workflow_attachment(
        mid, {"filename": "new", "mime": "image/png", "data": data, "workflow_id": "wf", **extra}
    )


async def _set_budget(db, bytes_limit: int) -> None:
    await db.execute("UPDATE settings SET attachment_cache_budget_bytes = ? WHERE id = 1", (bytes_limit,))
    await db.commit()


async def _counter(db) -> int:
    return (await db.one("SELECT attachment_access_counter FROM settings WHERE id = 1"))["attachment_access_counter"]


async def _accesses(db, by_id: dict[int, list[int]]) -> None:
    for aid, accesses in by_id.items():
        await db.execute("UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?", (json.dumps(accesses), aid))
    await db.commit()


async def _evicted(aid: int) -> bool:
    return (await must_get_workflow_attachment(aid))["data_b64"] == EVICTED_MARKER


async def test_get_budget_bytes_reads_settings_value(client, db):
    await _set_budget(db, 12345)
    assert await _get_budget_bytes_on(db) == 12345


async def test_record_access_advances_the_counter_once_per_id(client, db):
    _, mid = await _seed_message(client)
    aid = await _seed_row(mid)
    before = await _counter(db)
    await record_access([])
    assert await _counter(db) == before
    await record_access([aid])
    assert await _counter(db) == before + 1
    # A missing id still consumes a slot; only the real row is updated.
    await record_access([aid, 999999])
    assert await _counter(db) == before + 3


async def test_record_access_assigns_counters_in_input_order(client, db):
    _, mid = await _seed_message(client)
    ids = [await _seed_row(mid, data=b"%d" % i) for i in range(3)]
    await db.execute("UPDATE settings SET attachment_access_counter = 100 WHERE id = 1")
    await db.execute("UPDATE workflow_attachments SET recent_accesses = NULL")
    await db.commit()

    await record_access(ids)

    rows = await db.all("SELECT id, recent_accesses FROM workflow_attachments WHERE id IN (?, ?, ?) ORDER BY id", tuple(ids))
    assert [json.loads(r["recent_accesses"]) for r in rows] == [[101], [102], [103]]
    assert await _counter(db) == 103


async def test_record_access_trims_to_three(client, db):
    _, mid = await _seed_message(client)
    aid = await _seed_row(mid)
    await _accesses(db, {aid: [5, 4, 3]})
    await db.execute("UPDATE settings SET attachment_access_counter = 100 WHERE id = 1")
    await db.commit()
    await record_access([aid])
    # New value first (101), then v1=5, v2=4; v3=3 dropped off the tail.
    assert json.loads((await must_get_workflow_attachment(aid))["recent_accesses"]) == [101, 5, 4]


async def test_evict_sets_sentinel_preserves_other_columns_and_is_idempotent(client, db):
    _, mid = await _seed_message(client)
    aid = await _seed_row(mid)
    before = await must_get_workflow_attachment(aid)
    await evict(aid)
    await evict(aid)
    after = await must_get_workflow_attachment(aid)
    assert after["data_b64"] == EVICTED_MARKER
    for col in ("filename", "mime_type", "workflow_id", "parent_attachment_id", "annotation"):
        assert after[col] == before[col], f"column {col} changed during evict"


async def test_insert_birth_is_one_access(client, db):
    _, mid = await _seed_message(client)
    before = await _counter(db)
    new_id, _ = await _insert(mid, b"BIRTH")
    assert new_id is not None
    assert len(json.loads((await must_get_workflow_attachment(new_id))["recent_accesses"])) == 1
    assert await _counter(db) - before == 1


@pytest.mark.parametrize(
    "size,accesses,budget,evicted",
    [
        (10, [1, 999], 25, [True, False]),  # one row must go to fit a new 10-byte row
        (5, [1, 2, 999], 10, [True, True, False]),  # two rows must go to fit a new 5-byte row
    ],
)
async def test_insert_workflow_attachment_evicts_lowest_lru_rows_until_it_fits(client, db, size, accesses, budget, evicted):
    _, mid = await _seed_message(client)
    ids = [await _seed_row(mid, data=bytes([65 + i]) * size) for i in range(len(accesses))]
    await _accesses(db, dict(zip(ids, ([n] for n in accesses))))
    await _set_budget(db, budget)
    new_id, _ = await _insert(mid, b"N" * size)
    assert [await _evicted(aid) for aid in ids] == evicted, "lowest-access rows go first; the highest is protected"
    assert new_id is not None and not await _evicted(new_id), "new row inserted with bytes"


async def test_an_oversize_attachment_without_recovery_metadata_is_rejected_without_evicting(client, db):
    _, mid = await _seed_message(client)
    existing = await _seed_row(mid, data=b"KEEP-ME")
    # Budget = 1 byte; the new row lacks seed+generation_metadata, so rejection returns before any eviction.
    await _set_budget(db, 1)
    new_id, rejected = await insert_workflow_attachment(
        mid, {"filename": "huge", "mime": "image/png", "data": b"HHHHH", "workflow_id": "wf"}
    )
    assert new_id is None and rejected is not None
    assert {k: rejected[k] for k in ("filename", "mime", "data", "workflow_id", "reason")} == {
        "filename": "huge",
        "mime": "image/png",
        "data": b"HHHHH",
        "workflow_id": "wf",
        "reason": OVERSIZE_NO_METADATA_REASON,
    }
    assert not await _evicted(existing), "rejection must not have evicted real data"
    # Non-serializable generation metadata is no recovery metadata either.
    new_id, rejected = await _insert(mid, b"HHHHH", seed="seed", generation_metadata={"bad": {1, 2, 3}})
    assert new_id is None and rejected is not None and rejected["reason"] == OVERSIZE_NO_METADATA_REASON


async def test_insert_workflow_attachment_oversize_rehydratable_inserts_as_marker(client, db):
    _, mid = await _seed_message(client)
    existing = await _seed_row(mid, data=b"KEEP-ME")
    # The new row carries seed+generation_metadata, so the cache marker-inserts it; it stores no bytes, so nothing is evicted.
    await _set_budget(db, 1)
    new_id, _ = await _insert(mid, b"HHHHH", seed="test-seed", generation_metadata={})
    assert new_id is not None and await _evicted(new_id), "rehydratable oversize stored as marker"
    assert not await _evicted(existing), "marker insert must not evict existing real bytes"


async def test_non_rehydratable_existing_row_is_not_an_eviction_candidate(client, db):
    _, mid = await _seed_message(client)
    pinned = await _seed_row(mid, data=b"PINNED", recoverable=False)
    evictable = await _seed_row(mid, data=b"OLD", recoverable=True)
    await _accesses(db, {pinned: [1], evictable: [2]})
    await _set_budget(db, 7)
    new_id, rejected = await _insert(mid, b"NEW", seed="new-seed", generation_metadata={})
    assert new_id is not None and rejected is None
    assert not await _evicted(pinned)
    assert await _evicted(evictable)


async def test_explicit_evict_refuses_to_destroy_unrecoverable_bytes(client):
    _, mid = await _seed_message(client)
    aid = await _seed_row(mid, data=b"ONLY-COPY", recoverable=False)
    with pytest.raises(ValueError, match="no usable recovery metadata"):
        await evict(aid)
    assert not await _evicted(aid)


async def test_rehydrate_refuses_when_only_unrecoverable_bytes_could_make_room(client, db):
    _, mid = await _seed_message(client)
    target = await _seed_row(mid, data=b"TARGET")
    await evict(target)
    pinned = await _seed_row(mid, data=b"PINNED", recoverable=False)
    await _set_budget(db, 6)
    with pytest.raises(ValueError, match="cannot fit without evicting unrecoverable artifacts"):
        await rehydrate_attachment(target, b"NEW")
    assert await _evicted(target)
    assert not await _evicted(pinned)


@pytest.mark.parametrize("mark_active", [True, False])
async def test_insert_sibling_marks_the_root_active_unless_told_not_to(client, mark_active):
    _, mid = await _seed_message(client)
    root_id = await _seed_row(mid)
    new_id, _ = await insert_workflow_attachment(
        mid,
        {"filename": "sib", "mime": "image/png", "data": b"S", "workflow_id": "wf", "parent_attachment_id": root_id},
        mark_active=mark_active,
    )
    assert new_id != root_id
    assert (await must_get_workflow_attachment(root_id))["active_sibling_id"] == (new_id if mark_active else None)


async def test_insert_workflow_attachment_root_insert_does_not_touch_active(client):
    _, mid = await _seed_message(client)
    new_id, _ = await _insert(mid, b"R")
    assert new_id is not None
    assert (await must_get_workflow_attachment(new_id))["active_sibling_id"] is None


async def test_insert_workflow_attachment_policy_gate_unregistered_workflow(client, db):
    _, mid = await _seed_message(client)
    existing = await _seed_row(mid, data=b"KEEP")
    await _set_budget(db, 100)
    new_id, rejected = await insert_workflow_attachment(
        mid, {"filename": "x.bin", "mime": "image/png", "data": b"X", "workflow_id": "stale"}
    )
    assert new_id is None and rejected is not None
    assert (rejected["filename"], rejected["workflow_id"]) == ("x.bin", "stale")
    assert rejected["reason"] == WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON
    assert not await _evicted(existing)
    assert await db.all("SELECT id FROM workflow_attachments WHERE workflow_id = ?", ("stale",)) == [], (
        "policy-rejected attachment must not persist"
    )


async def test_insert_workflow_attachment_rejects_foreign_message_parent(client):
    cid = await _new_conversation(client)
    mid_a, _ = await add_message(cid, "assistant", "scene A", 0)
    mid_b, _ = await add_message(cid, "assistant", "scene B", 1, parent_id=mid_a)
    await set_active_leaf(cid, mid_b)
    root_on_a = await _seed_row(mid_a)
    with pytest.raises(ValueError, match="belongs to message"):
        await _insert(mid_b, parent_attachment_id=root_on_a)
    foreign_root = await must_get_workflow_attachment(root_on_a)
    assert foreign_root["active_sibling_id"] is None, "cross-message rejection must not write the foreign root's active pointer"


async def test_rehydrate_attachment_refuses_present_bytes_and_missing_rows(client):
    _, mid = await _seed_message(client)
    aid = await _seed_row(mid, data=b"PRESENT")
    with pytest.raises(ValueError, match="bytes are present"):
        await rehydrate_attachment(aid, b"NEW")
    with pytest.raises(LookupError):
        await rehydrate_attachment(999999, b"NEW")


async def test_rehydrate_attachment_writes_bytes_back_as_an_access(client, db):
    _, mid = await _seed_message(client)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x",
            "mime": "application/octet-stream",
            "data": b"ORIGINAL",
            "workflow_id": "wf",
            "seed": "s",
            "generation_metadata": {"steps": 7},
        },
    )
    await evict(aid)
    before = await _counter(db)
    await rehydrate_attachment(aid, b"RESTORED_BYTES")
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] == base64.b64encode(b"RESTORED_BYTES").decode("ascii")
    assert await _counter(db) - before == 1

    await evict(aid)
    await rehydrate_attachment(aid, b"NEW", consumption_metadata={"x": 1})
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] == base64.b64encode(b"NEW").decode("ascii")
    assert json.loads(row["consumption_metadata"]) == {"x": 1}
    assert json.loads(row["generation_metadata"]) == {"steps": 7}, "rehydrate never mutates generation metadata"


async def test_set_active_sibling_writes_and_clears_only_the_pointer(client):
    _, mid = await _seed_message(client)
    root_id = await _seed_row(mid)
    sib_id = await _seed_row(mid, parent=root_id)
    before = await must_get_workflow_attachment(root_id)
    await set_active_sibling(root_id, sib_id)
    after = await must_get_workflow_attachment(root_id)
    assert after["active_sibling_id"] == sib_id
    for col in ("data_b64", "filename", "mime_type", "workflow_id", "annotation", "seed"):
        assert before[col] == after[col]
    await set_active_sibling(root_id, None)
    assert (await must_get_workflow_attachment(root_id))["active_sibling_id"] is None
