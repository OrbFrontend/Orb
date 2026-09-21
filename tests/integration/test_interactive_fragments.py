"""Integration tests for interactive fragments CRUD API and DB persistence."""

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
    "cooldown_turns": 3,
}


async def test_list_interactive_fragments_returns_seeded_data(client, db):
    resp = await client.get("/api/interactive-fragments")
    assert resp.status_code == 200
    fragments = resp.json()
    ids = {f["id"] for f in fragments}
    assert "keywords" in ids
    assert "next_event" in ids
    assert "detected_repetitions" in ids
    assert "user_intent" in ids


async def test_create_interactive_fragment_persists_to_db(client, db):
    resp = await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "pacing"
    assert body["label"] == "Pacing"
    assert body["injection_label"] == "Pacing"
    assert body["field_type"] == "string"
    assert body["cooldown_turns"] == 3

    async with db.execute("SELECT * FROM interactive_fragments WHERE id = 'pacing'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["label"] == "Pacing"
    assert row["injection_label"] == "Pacing"
    assert row["field_type"] == "string"
    assert row["cooldown_turns"] == 3


async def test_create_duplicate_interactive_fragment_returns_400(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    resp = await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    assert resp.status_code == 400


async def test_create_interactive_fragment_with_array_type(client, db):
    payload = {**_BASE_PAYLOAD, "id": "custom-list", "field_type": "array"}
    resp = await client.post("/api/interactive-fragments", json=payload)
    assert resp.status_code == 200
    assert resp.json()["field_type"] == "array"


async def test_interactive_fragment_cooldown_is_bounded(client, db):
    for value in (-1, 51):
        response = await client.post(
            "/api/interactive-fragments",
            json={**_BASE_PAYLOAD, "id": f"invalid-{value}", "cooldown_turns": value},
        )
        assert response.status_code == 422


async def test_update_interactive_fragment_persists_to_db(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    resp = await client.put(
        "/api/interactive-fragments/pacing",
        json={"label": "Scene Pacing", "injection_label": "Scene pacing", "cooldown_turns": 5},
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "Scene Pacing"
    assert resp.json()["injection_label"] == "Scene pacing"
    assert resp.json()["cooldown_turns"] == 5

    async with db.execute(
        "SELECT label, injection_label, cooldown_turns FROM interactive_fragments WHERE id = 'pacing'"
    ) as cur:
        row = await cur.fetchone()
    assert row["label"] == "Scene Pacing"
    assert row["injection_label"] == "Scene pacing"
    assert row["cooldown_turns"] == 5


async def test_update_enabled_flag(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    resp = await client.put("/api/interactive-fragments/pacing", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] in (False, 0)


async def test_update_nonexistent_interactive_fragment_returns_404(client, db):
    resp = await client.put("/api/interactive-fragments/ghost", json={"label": "Ghost"})
    assert resp.status_code == 404


async def test_reorder_interactive_fragments_updates_a_lane_atomically(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    await client.post("/api/interactive-fragments", json={**_BASE_PAYLOAD, "id": "focus", "label": "Focus", "sort_order": 11})

    response = await client.put(
        "/api/interactive-fragments/reorder",
        json={"items": [{"id": "pacing", "sort_order": 11}, {"id": "focus", "sort_order": 10}]},
    )

    assert response.status_code == 200
    async with db.execute(
        "SELECT id, sort_order FROM interactive_fragments WHERE id IN ('pacing', 'focus') ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    assert [(row["id"], row["sort_order"]) for row in rows] == [("focus", 10), ("pacing", 11)]


async def test_reorder_interactive_fragments_rejects_a_missing_item_without_partial_update(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)

    response = await client.put(
        "/api/interactive-fragments/reorder",
        json={"items": [{"id": "pacing", "sort_order": 99}, {"id": "gone", "sort_order": 10}]},
    )

    assert response.status_code == 404
    async with db.execute("SELECT sort_order FROM interactive_fragments WHERE id = 'pacing'") as cur:
        row = await cur.fetchone()
    assert row["sort_order"] == 10


async def test_reorder_interactive_fragments_rejects_mixed_lanes(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)

    response = await client.put(
        "/api/interactive-fragments/reorder",
        json={"items": [{"id": "pacing", "sort_order": 5}, {"id": "suggested_actions", "sort_order": 10}]},
    )

    assert response.status_code == 422
    async with db.execute("SELECT sort_order FROM interactive_fragments WHERE id = 'pacing'") as cur:
        row = await cur.fetchone()
    assert row["sort_order"] == 10


async def test_delete_interactive_fragment_removes_from_db(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    resp = await client.delete("/api/interactive-fragments/pacing")
    assert resp.status_code == 200

    async with db.execute("SELECT id FROM interactive_fragments WHERE id = 'pacing'") as cur:
        row = await cur.fetchone()
    assert row is None


async def test_delete_nonexistent_interactive_fragment_returns_404(client, db):
    resp = await client.delete("/api/interactive-fragments/does-not-exist")
    assert resp.status_code == 404


async def test_seeded_interactive_fragments_have_correct_field_types(client, db):
    resp = await client.get("/api/interactive-fragments")
    assert resp.status_code == 200
    frags = {f["id"]: f for f in resp.json()}

    assert frags["user_intent"]["field_type"] == "string"
    assert frags["keywords"]["field_type"] == "array"
    assert frags["next_event"]["field_type"] == "string"
    assert frags["detected_repetitions"]["field_type"] == "array"


async def test_seeded_required_flags(client, db):
    resp = await client.get("/api/interactive-fragments")
    frags = {f["id"]: f for f in resp.json()}

    # Required seeded fragments
    for fid in ("keywords", "next_event"):
        assert frags[fid]["required"] in (True, 1), f"{fid} should be required"

    # Optional seeded fragments
    for fid in ("user_intent", "detected_repetitions"):
        assert frags[fid]["required"] in (False, 0), f"{fid} should be optional"


async def test_list_returns_sorted_by_sort_order(client, db):
    resp = await client.get("/api/interactive-fragments")
    frags = resp.json()
    orders = [f["sort_order"] for f in frags]
    assert orders == sorted(orders)
