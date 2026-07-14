from __future__ import annotations

import base64
import json

import pytest

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
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

from ._fixtures import make_workflow, must_get_workflow_attachment, register_for_test


@pytest.fixture(autouse=True)
def _register_wf_workflow():
    """Register the ``"wf"`` workflow with produces_artifacts=True for every
    test in this module. The cache helpers gate on producer-workflow
    registration; without this fixture every helper call would short-circuit
    to the policy-rejection path and the oversize/eviction behaviors under
    test would never run."""
    wf = make_workflow(
        "wf",
        produces_artifacts=True,
        regenerate=lambda ctx, body: [],
        reroll_gen=lambda ctx, params, seed: b"",
    )
    with register_for_test(wf):
        yield


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "Cache test"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_message(client) -> tuple[str, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    return cid, mid


async def _seed_row(mid: int, *, wid: str = "wf", data: bytes = b"X", parent: int | None = None) -> int:
    att = {"filename": "x", "mime": "application/octet-stream", "data": data, "workflow_id": wid}
    if parent is not None:
        att["parent_attachment_id"] = parent
    return await insert_workflow_attachment_row(mid, att)


async def _set_budget(db, bytes_limit: int) -> None:
    await db.execute("UPDATE settings SET attachment_cache_budget_bytes = ? WHERE id = 1", (bytes_limit,))
    await db.commit()


async def test_get_budget_bytes_reads_settings_value(client, db):
    await _set_budget(db, 12345)
    assert await _get_budget_bytes_on(db) == 12345


async def test_record_access_no_ids_no_counter_advance(client, db):
    before_rows = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))
    before = before_rows[0]["attachment_access_counter"]
    await record_access([])
    after_rows = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))
    assert after_rows[0]["attachment_access_counter"] == before


async def test_record_access_assigns_counters_in_input_order(client, db):
    cid, mid = await _seed_message(client)
    ids = [await _seed_row(mid, data=b"%d" % i) for i in range(3)]
    # Reset counter so values are deterministic.
    await db.execute("UPDATE settings SET attachment_access_counter = 100 WHERE id = 1")
    await db.execute("UPDATE workflow_attachments SET recent_accesses = NULL")
    await db.commit()

    await record_access(ids)

    rows = list(
        await db.execute_fetchall(
            "SELECT id, recent_accesses FROM workflow_attachments WHERE id IN (?, ?, ?) ORDER BY id",
            tuple(ids),
        )
    )
    parsed = {r["id"]: json.loads(r["recent_accesses"]) for r in rows}
    assert parsed[ids[0]] == [101]
    assert parsed[ids[1]] == [102]
    assert parsed[ids[2]] == [103]
    counter_after = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))
    assert counter_after[0]["attachment_access_counter"] == 103


async def test_record_access_trims_to_three(client, db):
    cid, mid = await _seed_message(client)
    aid = await _seed_row(mid)
    # Seed three entries directly, oldest last; bump counter past their values.
    await db.execute("UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?", (json.dumps([5, 4, 3]), aid))
    await db.execute("UPDATE settings SET attachment_access_counter = 100 WHERE id = 1")
    await db.commit()

    await record_access([aid])
    row = await must_get_workflow_attachment(aid)
    parsed = json.loads(row["recent_accesses"])
    assert len(parsed) == 3
    # New value first (101), then v1=5, v2=4; v3=3 dropped off the tail.
    assert parsed[0] == 101
    assert parsed[1:] == [5, 4]


async def test_record_access_skips_missing_ids(client, db):
    cid, mid = await _seed_message(client)
    aid = await _seed_row(mid)
    counter_before = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    await record_access([aid, 999999])  # 999999 doesn't exist
    counter_after = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    # Counter still advances by 2 (the missing id consumes a slot); only the real row is updated.
    assert counter_after - counter_before == 2


async def test_record_access_counter_survives_reload(client, db):
    cid, mid = await _seed_message(client)
    aid = await _seed_row(mid)
    counter_before = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    await record_access([aid])
    counter_after = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    assert counter_after == counter_before + 1


async def test_evict_sets_sentinel_and_preserves_other_columns(client, db):
    cid, mid = await _seed_message(client)
    aid = await _seed_row(mid)
    before = await must_get_workflow_attachment(aid)
    await evict(aid)
    after = await must_get_workflow_attachment(aid)
    assert after["data_b64"] == EVICTED_MARKER
    for col in ("filename", "mime_type", "workflow_id", "parent_attachment_id", "annotation"):
        assert after[col] == before[col], f"column {col} changed during evict"


async def test_evict_is_noop_on_already_evicted_row(client):
    cid, mid = await _seed_message(client)
    aid = await _seed_row(mid)
    await evict(aid)
    await evict(aid)
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] == EVICTED_MARKER


async def test_insert_workflow_attachment_birth_recent_accesses_has_one_entry(client):
    cid, mid = await _seed_message(client)
    new_id, _ = await insert_workflow_attachment(
        mid,
        {"filename": "x", "mime": "image/png", "data": b"BIRTH", "workflow_id": "wf"},
    )
    assert new_id is not None
    row = await must_get_workflow_attachment(new_id)
    parsed = json.loads(row["recent_accesses"])
    assert len(parsed) == 1


async def test_insert_workflow_attachment_birth_advances_counter_by_one(client, db):
    cid, mid = await _seed_message(client)
    before = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    await insert_workflow_attachment(
        mid,
        {"filename": "x", "mime": "image/png", "data": b"BIRTH", "workflow_id": "wf"},
    )
    after = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    assert after - before == 1


async def test_insert_workflow_attachment_evicts_lowest_lru_when_over_budget(client, db):
    cid, mid = await _seed_message(client)
    a1 = await _seed_row(mid, data=b"AAAAAAAAAA")  # 10 bytes
    a2 = await _seed_row(mid, data=b"BBBBBBBBBB")
    await db.execute("UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?", (json.dumps([1]), a1))
    await db.execute("UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?", (json.dumps([999]), a2))
    await db.commit()

    # Set budget so adding new 10 bytes requires evicting one row (10+10+10 > 25).
    await _set_budget(db, 25)

    new_id, _ = await insert_workflow_attachment(
        mid,
        {"filename": "new", "mime": "image/png", "data": b"NNNNNNNNNN", "workflow_id": "wf"},
    )
    assert new_id is not None
    r1 = await must_get_workflow_attachment(a1)
    r2 = await must_get_workflow_attachment(a2)
    r_new = await must_get_workflow_attachment(new_id)
    assert r1["data_b64"] == EVICTED_MARKER, "lowest-access row should be evicted"
    assert r2["data_b64"] != EVICTED_MARKER, "highest-access row should be retained"
    assert r_new["data_b64"] != EVICTED_MARKER, "new row inserted with bytes"


async def test_insert_workflow_attachment_evicts_multiple_when_needed(client, db):
    cid, mid = await _seed_message(client)
    a1 = await _seed_row(mid, data=b"AAAAA")  # 5 bytes
    a2 = await _seed_row(mid, data=b"BBBBB")
    a3 = await _seed_row(mid, data=b"CCCCC")
    await db.execute("UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?", (json.dumps([1]), a1))
    await db.execute("UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?", (json.dumps([2]), a2))
    await db.execute("UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?", (json.dumps([999]), a3))
    await db.commit()
    # Budget so two rows must be evicted to fit a new 5-byte row.
    await _set_budget(db, 10)

    await insert_workflow_attachment(
        mid,
        {"filename": "new", "mime": "image/png", "data": b"NNNNN", "workflow_id": "wf"},
    )
    r1 = await must_get_workflow_attachment(a1)
    r2 = await must_get_workflow_attachment(a2)
    r3 = await must_get_workflow_attachment(a3)
    assert r1["data_b64"] == EVICTED_MARKER
    assert r2["data_b64"] == EVICTED_MARKER
    assert r3["data_b64"] != EVICTED_MARKER, "highest-access row protected"


async def test_insert_workflow_attachment_self_oversized_returns_rejection_without_evicting(client, db):
    cid, mid = await _seed_message(client)
    # Seed an existing byte-bearing row so we can verify the refusal does
    # not evict anything in its attempt to make room.
    existing = await _seed_row(mid, data=b"KEEP-ME")
    # Budget = 1 byte; new row is 5 bytes AND lacks seed+generation_metadata
    # so it cannot be marker-inserted -- rejection returns before any
    # eviction, so the existing row stays byte-bearing.
    await _set_budget(db, 1)
    att_dict = {"filename": "huge", "mime": "image/png", "data": b"HHHHH", "workflow_id": "wf"}
    new_id, rejected = await insert_workflow_attachment(mid, att_dict)
    assert new_id is None
    assert rejected is not None
    assert rejected["filename"] == "huge"
    assert rejected["mime"] == "image/png"
    assert rejected["data"] == b"HHHHH"
    assert rejected["workflow_id"] == "wf"
    assert rejected["reason"] == OVERSIZE_NO_METADATA_REASON
    row = await must_get_workflow_attachment(existing)
    assert row["data_b64"] != EVICTED_MARKER, "rejection must not have evicted real data"


async def test_insert_workflow_attachment_oversize_rehydratable_inserts_as_marker(client, db):
    cid, mid = await _seed_message(client)
    existing = await _seed_row(mid, data=b"KEEP-ME")
    # Budget = 1 byte; new row is 5 bytes BUT carries seed+generation_metadata
    # so the cache marker-inserts (recoverable later via rehydrate). Existing
    # row is preserved -- no eviction needed because the new row stores no bytes.
    await _set_budget(db, 1)
    new_id, _ = await insert_workflow_attachment(
        mid,
        {
            "filename": "huge",
            "mime": "image/png",
            "data": b"HHHHH",
            "workflow_id": "wf",
            "seed": "test-seed",
            "generation_metadata": {},
        },
    )
    assert new_id is not None
    new_row = await must_get_workflow_attachment(new_id)
    assert new_row["data_b64"] == EVICTED_MARKER, "rehydratable oversize stored as marker"
    existing_row = await must_get_workflow_attachment(existing)
    assert existing_row["data_b64"] != EVICTED_MARKER, "marker insert must not evict existing real bytes"


async def test_insert_workflow_attachment_mark_active_writes_root_pointer(client):
    cid, mid = await _seed_message(client)
    root_id = await _seed_row(mid)
    new_id, _ = await insert_workflow_attachment(
        mid,
        {
            "filename": "sib",
            "mime": "image/png",
            "data": b"S",
            "workflow_id": "wf",
            "parent_attachment_id": root_id,
        },
    )
    root = await must_get_workflow_attachment(root_id)
    assert root["active_sibling_id"] == new_id


async def test_insert_workflow_attachment_mark_active_false_does_not_write(client):
    cid, mid = await _seed_message(client)
    root_id = await _seed_row(mid)
    new_id, _ = await insert_workflow_attachment(
        mid,
        {
            "filename": "sib",
            "mime": "image/png",
            "data": b"S",
            "workflow_id": "wf",
            "parent_attachment_id": root_id,
        },
        mark_active=False,
    )
    root = await must_get_workflow_attachment(root_id)
    assert root["active_sibling_id"] is None
    assert new_id != root_id


async def test_insert_workflow_attachment_root_insert_does_not_touch_active(client):
    cid, mid = await _seed_message(client)
    new_id, _ = await insert_workflow_attachment(
        mid,
        {"filename": "r", "mime": "image/png", "data": b"R", "workflow_id": "wf"},
    )
    assert new_id is not None
    row = await must_get_workflow_attachment(new_id)
    assert row["active_sibling_id"] is None


async def test_insert_workflow_attachment_policy_gate_unregistered_workflow(client, db):
    cid, mid = await _seed_message(client)
    existing = await _seed_row(mid, data=b"KEEP")
    await _set_budget(db, 100)

    new_id, rejected = await insert_workflow_attachment(
        mid,
        {"filename": "x.bin", "mime": "image/png", "data": b"X", "workflow_id": "stale"},
    )
    assert new_id is None
    assert rejected is not None
    assert rejected["filename"] == "x.bin"
    assert rejected["workflow_id"] == "stale"
    assert rejected["reason"] == WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON

    existing_row = await must_get_workflow_attachment(existing)
    assert existing_row["data_b64"] != EVICTED_MARKER

    new_rows = list(
        await db.execute_fetchall(
            "SELECT id FROM workflow_attachments WHERE workflow_id = ?",
            ("stale",),
        )
    )
    assert new_rows == [], "policy-rejected attachment must not persist"


async def test_insert_workflow_attachment_rejects_foreign_message_parent(client):
    cid = await _new_conversation(client)
    mid_a, _ = await add_message(cid, "assistant", "scene A", 0)
    mid_b, _ = await add_message(cid, "assistant", "scene B", 1, parent_id=mid_a)
    await set_active_leaf(cid, mid_b)
    root_on_a = await _seed_row(mid_a)
    with pytest.raises(ValueError, match="belongs to message"):
        await insert_workflow_attachment(
            mid_b,
            {
                "filename": "sib",
                "mime": "image/png",
                "data": b"S",
                "workflow_id": "wf",
                "parent_attachment_id": root_on_a,
            },
        )
    foreign_root = await must_get_workflow_attachment(root_on_a)
    assert foreign_root["active_sibling_id"] is None, "cross-message rejection must not write the foreign root's active pointer"


async def test_rehydrate_attachment_refuses_when_bytes_present(client):
    cid, mid = await _seed_message(client)
    aid = await _seed_row(mid, data=b"PRESENT")
    with pytest.raises(ValueError, match="bytes are present"):
        await rehydrate_attachment(aid, b"NEW")


async def test_rehydrate_attachment_lookup_error_when_missing(client):  # noqa: ARG001
    with pytest.raises(LookupError):
        await rehydrate_attachment(999999, b"NEW")


async def test_rehydrate_attachment_writes_bytes_back(client):
    cid, mid = await _seed_message(client)
    aid = await _seed_row(mid, data=b"ORIGINAL")
    await evict(aid)
    await rehydrate_attachment(aid, b"RESTORED_BYTES")
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] == base64.b64encode(b"RESTORED_BYTES").decode("ascii")


async def test_rehydrate_attachment_counts_as_access(client, db):
    cid, mid = await _seed_message(client)
    aid = await _seed_row(mid, data=b"OOO")
    await evict(aid)
    before = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    await rehydrate_attachment(aid, b"NEW")
    after = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    assert after - before == 1


async def test_rehydrate_attachment_writes_consumption_metadata_when_supplied(client):
    cid, mid = await _seed_message(client)
    aid = await _seed_row(mid, data=b"ORIGINAL")
    await evict(aid)
    await rehydrate_attachment(aid, b"NEW", consumption_metadata={"x": 1})
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] == base64.b64encode(b"NEW").decode("ascii")
    assert json.loads(row["consumption_metadata"]) == {"x": 1}


async def test_rehydrate_attachment_does_not_mutate_generation_metadata(client):
    cid, mid = await _seed_message(client)
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
    await rehydrate_attachment(aid, b"NEW", consumption_metadata={"x": 1})
    row = await must_get_workflow_attachment(aid)
    assert json.loads(row["generation_metadata"]) == {"steps": 7}


async def test_set_active_sibling_writes_value(client):
    cid, mid = await _seed_message(client)
    root_id = await _seed_row(mid)
    sib_id = await _seed_row(mid, parent=root_id)
    await set_active_sibling(root_id, sib_id)
    root = await must_get_workflow_attachment(root_id)
    assert root["active_sibling_id"] == sib_id


async def test_set_active_sibling_null_clears(client):
    cid, mid = await _seed_message(client)
    root_id = await _seed_row(mid)
    sib_id = await _seed_row(mid, parent=root_id)
    await set_active_sibling(root_id, sib_id)
    await set_active_sibling(root_id, None)
    root = await must_get_workflow_attachment(root_id)
    assert root["active_sibling_id"] is None


async def test_set_active_sibling_leaves_other_columns_intact(client):
    cid, mid = await _seed_message(client)
    root_id = await _seed_row(mid)
    sib_id = await _seed_row(mid, parent=root_id)
    before = await must_get_workflow_attachment(root_id)
    await set_active_sibling(root_id, sib_id)
    after = await must_get_workflow_attachment(root_id)
    for col in ("data_b64", "filename", "mime_type", "workflow_id", "annotation", "seed"):
        assert before[col] == after[col]
