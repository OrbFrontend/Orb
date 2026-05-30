from __future__ import annotations

import json

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)

from ._fixtures import must_get_workflow_attachment


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "access"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_attachment(client) -> tuple[str, int, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {"filename": "x", "mime": "image/png", "data": b"X", "workflow_id": "wf"},
    )
    return cid, mid, aid


async def test_unknown_conversation_returns_404(client):
    resp = await client.post(
        "/api/conversations/no-such/workflow-attachments/access",
        json={"ids": [1]},
    )
    assert resp.status_code == 404


async def test_ids_not_a_list_returns_400(client):
    cid = await _new_conversation(client)
    resp = await client.post(
        f"/api/conversations/{cid}/workflow-attachments/access",
        json={"ids": "not-a-list"},
    )
    assert resp.status_code == 400


async def test_empty_ids_returns_zero_recorded(client, db):
    cid = await _new_conversation(client)
    before = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    resp = await client.post(
        f"/api/conversations/{cid}/workflow-attachments/access",
        json={"ids": []},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "recorded": 0}
    after = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    assert after == before


async def test_valid_id_recorded(client, db):
    cid, mid, aid = await _seed_attachment(client)
    # Reset counter so the prepended value is deterministic.
    await db.execute("UPDATE settings SET attachment_access_counter = 100 WHERE id = 1")
    await db.commit()

    resp = await client.post(
        f"/api/conversations/{cid}/workflow-attachments/access",
        json={"ids": [aid]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "recorded": 1}
    row = await must_get_workflow_attachment(aid)
    parsed = json.loads(row["recent_accesses"])
    assert parsed[0] == 101


async def test_cross_conversation_id_dropped(client):
    cid_a, mid_a, aid_a = await _seed_attachment(client)
    cid_b, mid_b, aid_b = await _seed_attachment(client)
    resp = await client.post(
        f"/api/conversations/{cid_a}/workflow-attachments/access",
        json={"ids": [aid_a, aid_b]},
    )
    assert resp.json()["recorded"] == 1, "id from other conv is silently dropped"


async def test_duplicate_id_records_multiple_events(client):
    cid, mid, aid = await _seed_attachment(client)
    resp = await client.post(
        f"/api/conversations/{cid}/workflow-attachments/access",
        json={"ids": [aid, aid, aid]},
    )
    assert resp.json()["recorded"] == 3
    row = await must_get_workflow_attachment(aid)
    parsed = json.loads(row["recent_accesses"])
    # Three prepends, length capped at 3.
    assert len(parsed) == 3
    # Newest-first; all three should be distinct counter values, strictly descending.
    assert parsed[0] > parsed[1] > parsed[2]


async def test_bool_ids_dropped(client, db):
    cid, mid, aid = await _seed_attachment(client)
    before = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    resp = await client.post(
        f"/api/conversations/{cid}/workflow-attachments/access",
        json={"ids": [True, False, aid]},
    )
    assert resp.json()["recorded"] == 1, "bools are not ints for our purposes"
    after = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    assert after - before == 1


async def test_non_int_types_dropped(client):
    cid, mid, aid = await _seed_attachment(client)
    resp = await client.post(
        f"/api/conversations/{cid}/workflow-attachments/access",
        json={"ids": [1.5, "x", None, aid]},
    )
    assert resp.json()["recorded"] == 1


async def test_ordering_preserved_in_counter_assignment(client, db):
    """Earlier in the list -> smaller counter value."""
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    ids = [
        await insert_workflow_attachment_row(mid, {"filename": f"a{i}", "mime": "image/png", "data": b"X", "workflow_id": "wf"})
        for i in range(3)
    ]
    await db.execute("UPDATE settings SET attachment_access_counter = 0 WHERE id = 1")
    await db.execute("UPDATE workflow_attachments SET recent_accesses = NULL")
    await db.commit()

    await client.post(
        f"/api/conversations/{cid}/workflow-attachments/access",
        json={"ids": ids},
    )
    rows = list(
        await db.execute_fetchall(
            "SELECT id, recent_accesses FROM workflow_attachments WHERE id IN (?, ?, ?) ORDER BY id",
            tuple(ids),
        )
    )
    parsed = {r["id"]: json.loads(r["recent_accesses"])[0] for r in rows}
    assert parsed[ids[0]] < parsed[ids[1]] < parsed[ids[2]]
