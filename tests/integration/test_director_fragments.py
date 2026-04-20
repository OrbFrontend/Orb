"""Integration tests for director fragments CRUD API and DB persistence."""

from __future__ import annotations


_BASE_PAYLOAD = {
    "id": "pacing",
    "label": "Pacing",
    "description": "Describes the scene pacing.",
    "field_type": "string",
    "required": False,
    "enabled": True,
    "injection_label": "Pacing",
    "sort_order": 10,
}


async def test_list_director_fragments_returns_seeded_data(client, db):
    resp = await client.get("/api/director-fragments")
    assert resp.status_code == 200
    fragments = resp.json()
    ids = {f["id"] for f in fragments}
    assert "plot_summary" in ids
    assert "keywords" in ids
    assert "next_event" in ids
    assert "writing_direction" in ids
    assert "detected_repetitions" in ids
    assert "user_intent" in ids


async def test_create_director_fragment_persists_to_db(client, db):
    resp = await client.post("/api/director-fragments", json=_BASE_PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "pacing"
    assert body["label"] == "Pacing"
    assert body["injection_label"] == "Pacing"
    assert body["field_type"] == "string"

    async with db.execute("SELECT * FROM director_fragments WHERE id = 'pacing'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["label"] == "Pacing"
    assert row["injection_label"] == "Pacing"
    assert row["field_type"] == "string"


async def test_create_duplicate_director_fragment_returns_400(client, db):
    await client.post("/api/director-fragments", json=_BASE_PAYLOAD)
    resp = await client.post("/api/director-fragments", json=_BASE_PAYLOAD)
    assert resp.status_code == 400


async def test_create_director_fragment_with_array_type(client, db):
    payload = {**_BASE_PAYLOAD, "id": "custom-list", "field_type": "array"}
    resp = await client.post("/api/director-fragments", json=payload)
    assert resp.status_code == 200
    assert resp.json()["field_type"] == "array"


async def test_update_director_fragment_persists_to_db(client, db):
    await client.post("/api/director-fragments", json=_BASE_PAYLOAD)
    resp = await client.put(
        "/api/director-fragments/pacing",
        json={"label": "Scene Pacing", "injection_label": "Scene pacing"},
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "Scene Pacing"
    assert resp.json()["injection_label"] == "Scene pacing"

    async with db.execute("SELECT label, injection_label FROM director_fragments WHERE id = 'pacing'") as cur:
        row = await cur.fetchone()
    assert row["label"] == "Scene Pacing"
    assert row["injection_label"] == "Scene pacing"


async def test_update_enabled_flag(client, db):
    await client.post("/api/director-fragments", json=_BASE_PAYLOAD)
    resp = await client.put("/api/director-fragments/pacing", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] in (False, 0)


async def test_update_nonexistent_director_fragment_returns_404(client, db):
    resp = await client.put("/api/director-fragments/ghost", json={"label": "Ghost"})
    assert resp.status_code == 404


async def test_delete_director_fragment_removes_from_db(client, db):
    await client.post("/api/director-fragments", json=_BASE_PAYLOAD)
    resp = await client.delete("/api/director-fragments/pacing")
    assert resp.status_code == 200

    async with db.execute("SELECT id FROM director_fragments WHERE id = 'pacing'") as cur:
        row = await cur.fetchone()
    assert row is None


async def test_delete_nonexistent_director_fragment_returns_404(client, db):
    resp = await client.delete("/api/director-fragments/does-not-exist")
    assert resp.status_code == 404


async def test_seeded_director_fragments_have_correct_field_types(client, db):
    resp = await client.get("/api/director-fragments")
    assert resp.status_code == 200
    frags = {f["id"]: f for f in resp.json()}

    assert frags["plot_summary"]["field_type"] == "string"
    assert frags["user_intent"]["field_type"] == "string"
    assert frags["keywords"]["field_type"] == "array"
    assert frags["next_event"]["field_type"] == "string"
    assert frags["writing_direction"]["field_type"] == "string"
    assert frags["detected_repetitions"]["field_type"] == "array"


async def test_seeded_required_flags(client, db):
    resp = await client.get("/api/director-fragments")
    frags = {f["id"]: f for f in resp.json()}

    # Required seeded fragments
    for fid in ("plot_summary", "keywords", "next_event", "writing_direction"):
        assert frags[fid]["required"] in (True, 1), f"{fid} should be required"

    # Optional seeded fragments
    for fid in ("user_intent", "detected_repetitions"):
        assert frags[fid]["required"] in (False, 0), f"{fid} should be optional"


async def test_list_returns_sorted_by_sort_order(client, db):
    resp = await client.get("/api/director-fragments")
    frags = resp.json()
    orders = [f["sort_order"] for f in frags]
    assert orders == sorted(orders)
