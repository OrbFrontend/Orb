"""Integration tests for ``insert_workflow_attachments`` (batch path).

The batch entry point is what ``add_message`` uses to land workflow
artifacts atomically with the parent message INSERT.
"""

from __future__ import annotations

import json

import pytest

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.database.connection import get_db
from backend.workflows.attachment_cache import (
    EVICTED_MARKER,
    OVERSIZE_NO_METADATA_REASON,
    WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON,
    insert_workflow_attachments,
)

from ._fixtures import make_workflow, must_get_workflow_attachment, register_for_test


@pytest.fixture(autouse=True)
def _register_wf_workflow():
    """Register the ``"wf"`` workflow with produces_artifacts=True for every
    test in this module. Batch helper gates on producer-workflow registration;
    without this fixture every entry would land in the policy-rejection
    partition instead of exercising the oversize / eviction branches under
    test."""
    wf = make_workflow(
        "wf",
        produces_artifacts=True,
        regenerate=lambda ctx, body: [],
        reroll_gen=lambda ctx, params, seed: b"",
    )
    with register_for_test(wf):
        yield


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "Batch test"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_message(client) -> tuple[str, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    return cid, mid


async def _seed_row(mid: int, *, wid: str = "wf", data: bytes = b"X", recent: list[int] | None = None) -> int:
    rid = await insert_workflow_attachment_row(
        mid,
        {"filename": "x", "mime": "application/octet-stream", "data": data, "workflow_id": wid},
    )
    if recent is not None:
        async with get_db() as db:
            await db.execute(
                "UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?",
                (json.dumps(recent), rid),
            )
            await db.commit()
    return rid


async def _set_budget(db, bytes_limit: int) -> None:
    await db.execute("UPDATE settings SET attachment_cache_budget_bytes = ? WHERE id = 1", (bytes_limit,))
    await db.commit()


def _make_att(
    name: str,
    data: bytes,
    *,
    wid: str = "wf",
    parent: int | None = None,
    seed: str | None = None,
    generation_metadata: dict | None = None,
) -> dict:
    a: dict = {"filename": name, "mime": "image/png", "data": data, "workflow_id": wid}
    if parent is not None:
        a["parent_attachment_id"] = parent
    if seed is not None:
        a["seed"] = seed
    if generation_metadata is not None:
        a["generation_metadata"] = generation_metadata
    return a


async def test_empty_batch_returns_empty_list(client):
    cid, mid = await _seed_message(client)
    assert await insert_workflow_attachments(mid, []) == ([], [])


async def test_whole_batch_fits_no_eviction(client, db):
    cid, mid = await _seed_message(client)
    await _set_budget(db, 1000)

    new_ids, _ = await insert_workflow_attachments(
        mid,
        [
            _make_att("a", b"A" * 10),
            _make_att("b", b"B" * 20),
            _make_att("c", b"C" * 30),
        ],
    )
    assert len(new_ids) == 3
    for rid in new_ids:
        row = await must_get_workflow_attachment(rid)
        assert row["data_b64"] != EVICTED_MARKER


async def test_whole_batch_birth_as_access_for_every_row(client, db):
    cid, mid = await _seed_message(client)
    await _set_budget(db, 1000)

    new_ids, _ = await insert_workflow_attachments(
        mid,
        [_make_att(f"a{i}", b"X" * 5) for i in range(3)],
    )
    for rid in new_ids:
        row = await must_get_workflow_attachment(rid)
        parsed = json.loads(row["recent_accesses"])
        assert len(parsed) == 1, "every new row gets one birth access entry"


async def test_whole_batch_counter_advances_once_per_row(client, db):
    cid, mid = await _seed_message(client)
    await _set_budget(db, 1000)
    before = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    await insert_workflow_attachments(
        mid,
        [_make_att(f"a{i}", b"X") for i in range(4)],
    )
    after = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    assert after - before == 4


async def test_existing_evicted_oldest_first_to_make_headroom(client, db):
    cid, mid = await _seed_message(client)
    # Three existing 10-byte rows; oldest (lowest recent_accesses) at id1.
    a1 = await _seed_row(mid, data=b"A" * 10, recent=[1])
    a2 = await _seed_row(mid, data=b"B" * 10, recent=[2])
    a3 = await _seed_row(mid, data=b"C" * 10, recent=[999])
    # Budget = 40 bytes. Existing occupy 30. New batch = 20 bytes -> need 10.
    # Evict oldest (a1=10) -> done.
    await _set_budget(db, 40)
    new_ids, _ = await insert_workflow_attachments(
        mid,
        [_make_att("n1", b"N" * 10), _make_att("n2", b"M" * 10)],
    )
    r1 = await must_get_workflow_attachment(a1)
    r2 = await must_get_workflow_attachment(a2)
    r3 = await must_get_workflow_attachment(a3)
    assert r1["data_b64"] == EVICTED_MARKER, "oldest existing evicted"
    assert r2["data_b64"] != EVICTED_MARKER
    assert r3["data_b64"] != EVICTED_MARKER
    for rid in new_ids:
        row = await must_get_workflow_attachment(rid)
        assert row["data_b64"] != EVICTED_MARKER


async def test_existing_evicted_minimally_for_batch_total(client, db):
    cid, mid = await _seed_message(client)
    seeded = []
    for age in [1, 2, 3, 4, 999]:
        seeded.append(await _seed_row(mid, data=b"X" * 10, recent=[age]))
    # Budget = 50. Existing = 50. New = 30 -> need 30 -> evict three oldest.
    await _set_budget(db, 50)
    await insert_workflow_attachments(
        mid,
        [_make_att(f"n{i}", b"N" * 10) for i in range(3)],
    )
    evicted = []
    for rid in seeded:
        row = await must_get_workflow_attachment(rid)
        if row["data_b64"] == EVICTED_MARKER:
            evicted.append(rid)
    assert evicted == seeded[:3]


async def test_biggest_new_marked_preserves_existing_when_step_a_suffices(client, db):
    cid, mid = await _seed_message(client)
    e1 = await _seed_row(mid, data=b"X" * 10, recent=[1])
    # Budget = 15. Existing = 10 (fits). New batch = [50, 30, 5] = 85.
    # Step A walks biggest-first markering until new_byte_total <= 15:
    #   big (50) marker -> 35; mid (30) marker -> 5. Stop.
    # Step B: occupied (10) + new_byte_total (5) = 15 <= budget. No eviction.
    await _set_budget(db, 15)
    new_ids, _ = await insert_workflow_attachments(
        mid,
        [
            _make_att("big", b"B" * 50, seed="s1", generation_metadata={}),
            _make_att("mid", b"M" * 30, seed="s2", generation_metadata={}),
            _make_att("small", b"S" * 5, seed="s3", generation_metadata={}),
        ],
    )
    r_e1 = await must_get_workflow_attachment(e1)
    r_big = await must_get_workflow_attachment(new_ids[0])
    r_mid = await must_get_workflow_attachment(new_ids[1])
    r_small = await must_get_workflow_attachment(new_ids[2])
    assert r_e1["data_b64"] != EVICTED_MARKER, "existing preserved -- step A markers cover deficit"
    assert r_big["data_b64"] == EVICTED_MARKER
    assert r_mid["data_b64"] == EVICTED_MARKER
    assert r_small["data_b64"] != EVICTED_MARKER, "smallest new survives with bytes"


async def test_marker_row_stores_sentinel(client, db):
    cid, mid = await _seed_message(client)
    await _set_budget(db, 5)
    new_ids, _ = await insert_workflow_attachments(
        mid,
        [_make_att("huge", b"H" * 100, seed="s", generation_metadata={})],
    )
    row = await must_get_workflow_attachment(new_ids[0])
    assert row["data_b64"] == EVICTED_MARKER


async def test_marker_birth_as_access_still_records(client, db):
    cid, mid = await _seed_message(client)
    await _set_budget(db, 1)  # any non-trivial att triggers marker
    new_ids, _ = await insert_workflow_attachments(
        mid,
        [_make_att("huge", b"H" * 100, seed="s", generation_metadata={})],
    )
    row = await must_get_workflow_attachment(new_ids[0])
    assert row["data_b64"] == EVICTED_MARKER
    parsed = json.loads(row["recent_accesses"])
    assert len(parsed) == 1, "marker rows still get a birth access entry"


async def test_hopeless_batch_markers_all_new_and_keeps_existing_under_budget(client, db):
    cid, mid = await _seed_message(client)
    e1 = await _seed_row(mid, data=b"E" * 5, recent=[1])
    # Budget = 20. Existing = 5. New batch = [100, 80] = 180.
    # Step A: walk biggest-first markering until new_byte_total <= 20:
    #   100 marker -> 80; 80 marker -> 0. Stop.
    # Step B: occupied (5) + 0 = 5 <= budget. No eviction.
    await _set_budget(db, 20)
    new_ids, _ = await insert_workflow_attachments(
        mid,
        [
            _make_att("big", b"B" * 100, seed="s1", generation_metadata={}),
            _make_att("mid", b"M" * 80, seed="s2", generation_metadata={}),
        ],
    )
    r_e1 = await must_get_workflow_attachment(e1)
    assert r_e1["data_b64"] != EVICTED_MARKER, "existing under budget preserved"
    for rid in new_ids:
        row = await must_get_workflow_attachment(rid)
        assert row["data_b64"] == EVICTED_MARKER


async def test_runtime_over_budget_existing_evicted_to_converge(client, db):
    cid, mid = await _seed_message(client)
    # Existing occupies 4 bytes; budget then shrunk to 1 -- runtime over-budget.
    # The cache converges toward budget by evicting on the next write.
    existing = await _seed_row(mid, data=b"KEEP", recent=[10])
    await _set_budget(db, 1)
    new_ids, _ = await insert_workflow_attachments(
        mid,
        [_make_att("huge", b"H" * 100, seed="s", generation_metadata={})],
    )
    # Step A: huge (100) marker -> new_byte_total = 0.
    # Step B: occupied (4) + 0 - budget (1) = 3 > 0. Evict existing to converge.
    e_row = await must_get_workflow_attachment(existing)
    n_row = await must_get_workflow_attachment(new_ids[0])
    assert e_row["data_b64"] == EVICTED_MARKER, "over-budget existing evicted on next write"
    assert n_row["data_b64"] == EVICTED_MARKER


async def test_mark_active_per_att_with_parent(client):
    cid, mid = await _seed_message(client)
    root = await _seed_row(mid)
    new_ids, _ = await insert_workflow_attachments(
        mid,
        [_make_att("s1", b"S1", parent=root), _make_att("s2", b"S2", parent=root)],
    )
    r_root = await must_get_workflow_attachment(root)
    assert r_root["active_sibling_id"] == new_ids[-1], "last sibling of same root wins"


async def test_mark_active_off_skips_root_update(client):
    cid, mid = await _seed_message(client)
    root = await _seed_row(mid)
    await insert_workflow_attachments(
        mid,
        [_make_att("s1", b"S1", parent=root)],
        mark_active=False,
    )
    r_root = await must_get_workflow_attachment(root)
    assert r_root["active_sibling_id"] is None


async def test_root_inserts_dont_touch_active_pointer(client):
    cid, mid = await _seed_message(client)
    new_ids, _ = await insert_workflow_attachments(
        mid,
        [_make_att("r1", b"R1"), _make_att("r2", b"R2")],
    )
    for rid in new_ids:
        row = await must_get_workflow_attachment(rid)
        assert row["active_sibling_id"] is None


async def test_insert_workflow_attachments_rejects_foreign_message_parent(client):
    cid = await _new_conversation(client)
    mid_a, _ = await add_message(cid, "assistant", "scene A", 0)
    mid_b, _ = await add_message(cid, "assistant", "scene B", 1, parent_id=mid_a)
    await set_active_leaf(cid, mid_b)
    root_on_a = await _seed_row(mid_a)
    with pytest.raises(ValueError, match="belongs to message"):
        await insert_workflow_attachments(
            mid_b,
            [_make_att("sib", b"S", parent=root_on_a)],
        )
    foreign_root = await must_get_workflow_attachment(root_on_a)
    assert foreign_root["active_sibling_id"] is None, "cross-message rejection must not write the foreign root's active pointer"


async def test_insert_workflow_attachments_db_branch_requires_active_transaction(client):
    cid, mid = await _seed_message(client)
    async with get_db() as conn:
        assert conn.in_transaction is False
        with pytest.raises(RuntimeError, match="active write transaction"):
            await insert_workflow_attachments(
                mid,
                [_make_att("x", b"X")],
                db=conn,
            )


async def test_caller_owned_tx_rolls_back_on_failure(client, db):
    cid, mid = await _seed_message(client)
    existing = await _seed_row(mid, data=b"BEFORE")
    before = await must_get_workflow_attachment(existing)

    # Open our own tx; pass it to the batch helper; force a failure by
    # passing a malformed att that passes the policy gate (carries a
    # registered workflow_id) but trips the row helper's validation on
    # data/path absence.
    raised = False
    try:
        async with get_db() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await insert_workflow_attachments(
                mid,
                [
                    _make_att("ok", b"OK"),
                    {"filename": "bad", "mime": "image/png", "workflow_id": "wf"},  # no data / path
                ],
                db=conn,
            )
            await conn.commit()  # not reached
    except (ValueError, LookupError):
        raised = True

    assert raised
    after = await must_get_workflow_attachment(existing)
    assert after == before
    from backend.database import get_workflow_attachments_for_message

    rows = await get_workflow_attachments_for_message(mid)
    assert [r["id"] for r in rows] == [existing], "rollback discarded the partial batch"


async def test_batch_ids_in_input_order(client):
    cid, mid = await _seed_message(client)
    new_ids, _ = await insert_workflow_attachments(
        mid,
        [_make_att("a", b"A"), _make_att("b", b"B"), _make_att("c", b"C")],
    )
    # SQLite AUTOINCREMENT yields strictly increasing ids in insert order.
    assert new_ids[0] < new_ids[1] < new_ids[2]


async def test_add_message_oversize_non_rehydratable_drops_workflow_att(client, db):
    """A workflow att that exceeds the cache budget AND lacks seed +
    generation_metadata is dropped by the cache (not marker-inserted,
    not raised). The message + user atts still commit. add_message's
    return tuple's second element exposes the dropped att so the
    orchestrator can emit a SSE event surfacing the rejection."""
    cid = await _new_conversation(client)
    user_mid, _ = await add_message(cid, "user", "u", 0)
    await set_active_leaf(cid, user_mid)
    await _set_budget(db, 5)

    huge = {
        "source": "workflow:wf",
        "workflow_id": "wf",
        "filename": "huge.png",
        "mime": "image/png",
        "data": b"H" * 100,
        # no seed, no generation_metadata -> permanently unrecoverable as marker
    }
    asst_mid, rejected = await add_message(
        cid,
        "assistant",
        "draft",
        0,
        parent_id=user_mid,
        attachments=[huge],
    )

    asst_row = list(await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (asst_mid,)))
    assert len(asst_row) == 1
    # Workflow att dropped: no row at all (not even a marker).
    wf_rows = list(await db.execute_fetchall("SELECT * FROM workflow_attachments WHERE message_id = ?", (asst_mid,)))
    assert wf_rows == []
    assert len(rejected) == 1
    assert rejected[0]["filename"] == "huge.png"
    assert rejected[0]["reason"] == OVERSIZE_NO_METADATA_REASON


async def test_add_message_oversize_rehydratable_inserts_marker_atomic(client, db):
    """Rehydratable oversize (seed + generation_metadata present) still
    marker-inserts atomically with the message. The message + marker
    row commit together; rejected list is empty."""
    cid = await _new_conversation(client)
    user_mid, _ = await add_message(cid, "user", "u", 0)
    await set_active_leaf(cid, user_mid)
    await _set_budget(db, 5)

    huge = {
        "source": "workflow:wf",
        "workflow_id": "wf",
        "filename": "huge.png",
        "mime": "image/png",
        "data": b"H" * 100,
        "seed": "test-seed",
        "generation_metadata": {},
    }
    asst_mid, rejected = await add_message(
        cid,
        "assistant",
        "draft",
        0,
        parent_id=user_mid,
        attachments=[huge],
    )

    asst_row = list(await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (asst_mid,)))
    assert len(asst_row) == 1
    wf_rows = list(await db.execute_fetchall("SELECT * FROM workflow_attachments WHERE message_id = ?", (asst_mid,)))
    assert len(wf_rows) == 1
    assert wf_rows[0]["data_b64"] == EVICTED_MARKER
    assert rejected == []


async def test_add_message_workflow_atts_skipped_when_message_insert_fails(client, db):
    """If the message INSERT raises (e.g. FK violation on parent_id), the
    workflow batch is never reached, and nothing commits."""
    cid = await _new_conversation(client)
    before_rows = list(await db.execute_fetchall("SELECT COUNT(*) AS c FROM workflow_attachments"))
    before_count = before_rows[0]["c"]

    huge = {
        "source": "workflow:wf",
        "workflow_id": "wf",
        "filename": "h.png",
        "mime": "image/png",
        "data": b"X",
    }
    with pytest.raises(ValueError, match="Foreign key constraint"):
        await add_message(
            cid,
            "assistant",
            "draft",
            0,
            parent_id=99999,  # non-existent parent
            attachments=[huge],
        )

    after_rows = list(await db.execute_fetchall("SELECT COUNT(*) AS c FROM workflow_attachments"))
    assert after_rows[0]["c"] == before_count, "no workflow att leaked when message INSERT failed"


async def test_batch_policy_gate_unregistered_workflow_rejected_without_eviction(client, db):
    """An entry whose workflow_id is not registered with
    produces_artifacts=True is moved to rejected_atts with the policy
    reason. Its size is excluded from byte accounting -- existing
    byte-bearing rows are not evicted just to make room for an entry
    that won't insert."""
    cid, mid = await _seed_message(client)
    existing = await _seed_row(mid, data=b"KEEP", recent=[1])  # 4 bytes
    await _set_budget(db, 5)

    policy_rejected = _make_att("huge.bin", b"H" * 100, wid="stale")
    valid = _make_att("ok.bin", b"A", wid="wf")

    new_ids, rejected = await insert_workflow_attachments(mid, [policy_rejected, valid])

    assert len(new_ids) == 1
    new_row = await must_get_workflow_attachment(new_ids[0])
    assert new_row["workflow_id"] == "wf"

    assert len(rejected) == 1
    assert rejected[0]["filename"] == "huge.bin"
    assert rejected[0]["workflow_id"] == "stale"
    assert rejected[0]["reason"] == WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON

    existing_row = await must_get_workflow_attachment(existing)
    assert existing_row["data_b64"] != EVICTED_MARKER, (
        "policy-rejected entry must not be counted toward byte budget; "
        "existing byte-bearing row would have been evicted if its 100 "
        "bytes participated in Step A / Step B accounting"
    )


async def test_batch_policy_gate_all_unregistered_inserts_nothing(client):
    cid, mid = await _seed_message(client)
    atts = [
        _make_att("a.bin", b"AAAA", wid="stale-a"),
        _make_att("b.bin", b"BB", wid="stale-b"),
    ]
    new_ids, rejected = await insert_workflow_attachments(mid, atts)
    assert new_ids == []
    assert len(rejected) == 2
    assert {r["filename"] for r in rejected} == {"a.bin", "b.bin"}
    assert all(r["reason"] == WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON for r in rejected)
