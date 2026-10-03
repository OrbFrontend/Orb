"""Integration tests for interactive fragments CRUD API and DB persistence."""

from __future__ import annotations

import pytest

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


# Post-processing gates

_GATED = {
    **_BASE_PAYLOAD,
    "id": "trim",
    "field_type": "post_processing",
    "post_processing_gate": "Do more than two actions happen?",
}


async def test_post_processing_gate_is_created_or_defaults_empty(client, db):
    gated = await client.post("/api/interactive-fragments", json=_GATED)
    ungated = await client.post(
        "/api/interactive-fragments", json={**_BASE_PAYLOAD, "id": "plain", "field_type": "post_processing"}
    )

    assert gated.json()["post_processing_gate"] == "Do more than two actions happen?"
    assert ungated.json()["post_processing_gate"] == ""


async def test_post_processing_gate_rejects_explicit_null_and_overlong_text(client, db):
    null = await client.post("/api/interactive-fragments", json={**_GATED, "post_processing_gate": None})
    long = await client.post("/api/interactive-fragments", json={**_GATED, "post_processing_gate": "x" * 2001})

    assert null.status_code == 422
    assert long.status_code == 422


async def test_post_processing_gate_update_preserves_on_omission_or_null_and_clears_on_empty(client, db):
    await client.post("/api/interactive-fragments", json=_GATED)

    omitted = await client.put("/api/interactive-fragments/trim", json={"label": "Trim"})
    nulled = await client.put("/api/interactive-fragments/trim", json={"post_processing_gate": None})
    changed = await client.put("/api/interactive-fragments/trim", json={"post_processing_gate": "Is it long?"})
    cleared = await client.put("/api/interactive-fragments/trim", json={"post_processing_gate": ""})

    assert omitted.json()["post_processing_gate"] == "Do more than two actions happen?"
    assert nulled.json()["post_processing_gate"] == "Do more than two actions happen?"
    assert changed.json()["post_processing_gate"] == "Is it long?"
    assert cleared.json()["post_processing_gate"] == ""


async def test_other_types_never_store_a_gate(client, db):
    created = await client.post("/api/interactive-fragments", json={**_GATED, "id": "scene", "field_type": "string"})
    await client.post("/api/interactive-fragments", json=_GATED)
    retyped = await client.put("/api/interactive-fragments/trim", json={"field_type": "feedback"})
    partial = await client.put("/api/interactive-fragments/trim", json={"post_processing_gate": "Sneaky?"})
    back = await client.put("/api/interactive-fragments/trim", json={"field_type": "post_processing"})

    assert created.json()["post_processing_gate"] == ""
    assert retyped.json()["post_processing_gate"] == ""
    assert partial.json()["post_processing_gate"] == ""
    assert back.json()["post_processing_gate"] == ""


async def test_gate_history_is_bounded_kept_and_cleared_with_the_type(client, db):
    created = await client.post("/api/interactive-fragments", json={**_GATED, "post_processing_gate_replies": 2})
    over = await client.post("/api/interactive-fragments", json={**_GATED, "id": "x", "post_processing_gate_replies": 11})
    kept = await client.put("/api/interactive-fragments/trim", json={"label": "Trim"})
    retyped = await client.put("/api/interactive-fragments/trim", json={"field_type": "feedback"})

    assert created.json()["post_processing_gate_replies"] == 2
    assert over.status_code == 422
    assert kept.json()["post_processing_gate_replies"] == 2
    assert retyped.json()["post_processing_gate_replies"] == 0


async def test_direct_database_create_normalizes_a_missing_or_null_gate(client, db):
    from backend.database import create_interactive_fragment

    base = {key: value for key, value in _BASE_PAYLOAD.items() if key != "id"}
    missing = await create_interactive_fragment({**base, "id": "missing", "field_type": "post_processing"})
    null = await create_interactive_fragment(
        {**base, "id": "null", "field_type": "post_processing", "post_processing_gate": None}
    )

    assert missing is not None and missing["post_processing_gate"] == ""
    assert null is not None and null["post_processing_gate"] == ""


async def test_card_gate_survives_export_and_import(client, db, tmp_path):
    from backend.features.cards import parsing
    from backend.pipeline.context import _load_pipeline_context

    extensions = {
        "orb": {
            "fragments": {
                "interactive": [
                    {
                        "id": "card_trim",
                        "label": "Card Trim",
                        "description": "Trim actions.",
                        "field_type": "post_processing",
                        "post_processing_gate": "Do more than two actions happen?",
                        "post_processing_gate_replies": 2,
                    }
                ]
            }
        }
    }
    card = (await client.post("/api/characters", json={"name": "Gated", "extensions": extensions})).json()
    exported = tmp_path / "export.png"
    exported.write_bytes((await client.get(f"/api/characters/{card['id']}/export")).content)
    reimported = parsing.card_to_dict(parsing.parse(str(exported)))
    assert reimported["extensions"]["orb"]["fragments"] == extensions["orb"]["fragments"]

    copy = (await client.post("/api/characters", json={**reimported, "name": "Gated copy"})).json()
    conv = (await client.post("/api/conversations", json={"character_card_id": copy["id"]})).json()
    ctx = await _load_pipeline_context(conv["id"])
    assert ctx is not None
    fragment = next(row for row in ctx.interactive_fragments if row["id"] == "card_trim")
    assert fragment["post_processing_gate"] == "Do more than two actions happen?"
    assert fragment["post_processing_gate_replies"] == 2


def test_migration_0078_gives_existing_gates_no_history_and_reruns_safely():
    import importlib
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE interactive_fragments (id TEXT PRIMARY KEY, post_processing_gate TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO interactive_fragments VALUES ('trim', 'Is it long?')")
    migrate = importlib.import_module("backend.database.migrations.0078_post_processing_gate_replies").migrate

    migrate(conn)
    migrate(conn)

    assert conn.execute("SELECT post_processing_gate_replies FROM interactive_fragments").fetchall() == [(0,)]
    conn.close()


def test_migration_0072_adds_an_empty_gate_and_reruns_safely():
    import importlib
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE interactive_fragments (id TEXT PRIMARY KEY, label TEXT NOT NULL, field_type TEXT NOT NULL)")
    conn.execute("INSERT INTO interactive_fragments VALUES ('humanize', 'Humanize', 'post_processing')")
    migrate = importlib.import_module("backend.database.migrations.0072_post_processing_gate").migrate

    migrate(conn)
    migrate(conn)

    assert conn.execute("SELECT post_processing_gate FROM interactive_fragments").fetchall() == [("",)]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO interactive_fragments VALUES ('x', 'X', 'post_processing', NULL)")
    conn.close()


def test_migration_0072_skips_a_database_without_the_table():
    import importlib
    import sqlite3

    conn = sqlite3.connect(":memory:")
    importlib.import_module("backend.database.migrations.0072_post_processing_gate").migrate(conn)
    assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    conn.close()
