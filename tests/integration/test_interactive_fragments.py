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
    fragments = await client.get_json("/api/interactive-fragments")
    ids = {f["id"] for f in fragments}
    assert "keywords" in ids
    assert "next_event" in ids
    assert "detected_repetitions" in ids
    assert "user_intent" in ids


async def test_create_interactive_fragment_persists_to_db(client, db):
    body = await client.post_json("/api/interactive-fragments", json=_BASE_PAYLOAD)
    assert body["id"] == "pacing"
    assert body["label"] == "Pacing"
    assert body["injection_label"] == "Pacing"
    assert body["field_type"] == "string"
    assert body["cooldown_turns"] == 3

    row = await db.one("SELECT * FROM interactive_fragments WHERE id = 'pacing'")
    assert row is not None
    assert row["label"] == "Pacing"
    assert row["injection_label"] == "Pacing"
    assert row["field_type"] == "string"
    assert row["cooldown_turns"] == 3


async def test_create_duplicate_interactive_fragment_returns_400(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    await client.post_checked("/api/interactive-fragments", json=_BASE_PAYLOAD, expected_status=400)


async def test_create_interactive_fragment_with_array_type(client, db):
    payload = {**_BASE_PAYLOAD, "id": "custom-list", "field_type": "array"}
    assert (await client.post_json("/api/interactive-fragments", json=payload))["field_type"] == "array"


async def test_interactive_fragment_cooldown_rejects_negative(client, db):
    await client.post_checked(
        "/api/interactive-fragments", json={**_BASE_PAYLOAD, "id": "invalid", "cooldown_turns": -1}, expected_status=422
    )


async def test_update_interactive_fragment_persists_to_db(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    resp = await client.put_json(
        "/api/interactive-fragments/pacing",
        json={"label": "Scene Pacing", "injection_label": "Scene pacing", "cooldown_turns": 5},
    )
    assert resp["label"] == "Scene Pacing"
    assert resp["injection_label"] == "Scene pacing"
    assert resp["cooldown_turns"] == 5

    row = await db.one("SELECT label, injection_label, cooldown_turns FROM interactive_fragments WHERE id = 'pacing'")
    assert row["label"] == "Scene Pacing"
    assert row["injection_label"] == "Scene pacing"
    assert row["cooldown_turns"] == 5


async def test_update_enabled_flag(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    assert (await client.put_json("/api/interactive-fragments/pacing", json={"enabled": False}))["enabled"] in (False, 0)


async def test_update_nonexistent_interactive_fragment_returns_404(client, db):
    await client.put_checked("/api/interactive-fragments/ghost", json={"label": "Ghost"}, expected_status=404)


async def _stage_order(client, ids):
    listed = await client.get_json("/api/interactive-fragments")
    return [row["id"] for row in listed if row["id"] in ids]


async def test_reorder_spreads_tied_slots_so_the_asked_order_holds(client, db):
    ids = ["pacing", "focus", "aim"]
    for fid in ids:
        await client.post("/api/interactive-fragments", json={**_BASE_PAYLOAD, "id": fid, "label": fid, "sort_order": 0})
    before = [row["id"] for row in await client.get_json("/api/interactive-fragments") if row["id"] not in ids]

    await client.put_checked("/api/interactive-fragments/reorder", json={"ids": ["pacing", "aim", "focus"]})

    listed = await client.get_json("/api/interactive-fragments")
    assert [row["id"] for row in listed if row["id"] in ids] == ["pacing", "aim", "focus"]
    assert [row["id"] for row in listed if row["id"] not in ids] == before
    assert len({row["sort_order"] for row in listed}) == len(listed)


async def test_a_new_fragment_without_a_slot_goes_last(client, db):
    payload = {key: value for key, value in _BASE_PAYLOAD.items() if key != "sort_order"}
    await client.post("/api/interactive-fragments", json=payload)

    assert (await client.get_json("/api/interactive-fragments"))[-1]["id"] == "pacing"


async def test_reorder_interactive_fragments_rejects_a_missing_item_without_partial_update(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    await client.post("/api/interactive-fragments", json={**_BASE_PAYLOAD, "id": "focus", "label": "Focus", "sort_order": 11})

    await client.put_checked(
        "/api/interactive-fragments/reorder", json={"ids": ["focus", "gone", "pacing"]}, expected_status=404
    )

    assert await _stage_order(client, ["pacing", "focus"]) == ["pacing", "focus"]


async def test_reorder_interactive_fragments_rejects_mixed_stages(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)

    await client.put_checked(
        "/api/interactive-fragments/reorder", json={"ids": ["suggested_actions", "pacing"]}, expected_status=422
    )

    assert (await db.one("SELECT sort_order FROM interactive_fragments WHERE id = 'pacing'"))["sort_order"] == 10


async def test_delete_interactive_fragment_removes_from_db(client, db):
    await client.post("/api/interactive-fragments", json=_BASE_PAYLOAD)
    await client.delete_checked("/api/interactive-fragments/pacing")

    assert (await db.one("SELECT id FROM interactive_fragments WHERE id = 'pacing'")) is None


async def test_delete_nonexistent_interactive_fragment_returns_404(client, db):
    await client.delete_checked("/api/interactive-fragments/does-not-exist", expected_status=404)


async def test_seeded_interactive_fragments_have_correct_field_types(client, db):
    resp = await client.get_json("/api/interactive-fragments")
    frags = {f["id"]: f for f in resp}

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
    frags = (await client.get("/api/interactive-fragments")).json()
    orders = [f["sort_order"] for f in frags]
    assert orders == sorted(orders)


# Recast gates

_GATED = {
    **_BASE_PAYLOAD,
    "id": "trim",
    "field_type": "recast",
    "gate_question": "Do more than two actions happen?",
}


async def test_gate_question_is_created_or_defaults_empty(client, db):
    question = "x" * 4000
    gated = await client.post("/api/interactive-fragments", json={**_GATED, "gate_question": question})
    ungated = await client.post("/api/interactive-fragments", json={**_BASE_PAYLOAD, "id": "plain", "field_type": "recast"})

    assert gated.json()["gate_question"] == question
    assert ungated.json()["gate_question"] == ""


async def test_gate_question_rejects_explicit_null(client, db):
    null = await client.post("/api/interactive-fragments", json={**_GATED, "gate_question": None})

    assert null.status_code == 422


async def test_gate_question_update_preserves_on_omission_or_null_and_clears_on_empty(client, db):
    await client.post("/api/interactive-fragments", json=_GATED)

    question = "x" * 4000
    omitted = await client.put("/api/interactive-fragments/trim", json={"label": "Trim"})
    nulled = await client.put("/api/interactive-fragments/trim", json={"gate_question": None})
    changed = await client.put("/api/interactive-fragments/trim", json={"gate_question": question})
    cleared = await client.put("/api/interactive-fragments/trim", json={"gate_question": ""})

    assert omitted.json()["gate_question"] == "Do more than two actions happen?"
    assert nulled.json()["gate_question"] == "Do more than two actions happen?"
    assert changed.json()["gate_question"] == question
    assert cleared.json()["gate_question"] == ""


async def test_other_types_never_store_a_gate(client, db):
    created = await client.post("/api/interactive-fragments", json={**_GATED, "id": "scene", "field_type": "string"})
    await client.post("/api/interactive-fragments", json=_GATED)
    retyped = await client.put("/api/interactive-fragments/trim", json={"field_type": "feedback"})
    partial = await client.put("/api/interactive-fragments/trim", json={"gate_question": "Sneaky?"})
    back = await client.put("/api/interactive-fragments/trim", json={"field_type": "recast"})

    assert created.json()["gate_question"] == ""
    assert retyped.json()["gate_question"] == ""
    assert partial.json()["gate_question"] == ""
    assert back.json()["gate_question"] == ""


async def test_gate_history_is_bounded_kept_and_cleared_with_the_type(client, db):
    created = await client.post("/api/interactive-fragments", json={**_GATED, "gate_replies": 2})
    over = await client.post("/api/interactive-fragments", json={**_GATED, "id": "x", "gate_replies": 11})
    kept = await client.put("/api/interactive-fragments/trim", json={"label": "Trim"})
    retyped = await client.put("/api/interactive-fragments/trim", json={"field_type": "feedback"})

    assert created.json()["gate_replies"] == 2
    assert over.status_code == 422
    assert kept.json()["gate_replies"] == 2
    assert retyped.json()["gate_replies"] == 0


async def test_direct_database_create_normalizes_a_missing_or_null_gate(client, db):
    from backend.database import create_interactive_fragment

    base = {key: value for key, value in _BASE_PAYLOAD.items() if key != "id"}
    missing = await create_interactive_fragment({**base, "id": "missing", "field_type": "recast"})
    null = await create_interactive_fragment({**base, "id": "null", "field_type": "recast", "gate_question": None})

    assert missing is not None and missing["gate_question"] == ""
    assert null is not None and null["gate_question"] == ""


async def test_card_gate_survives_export_and_import(client, db, tmp_path):
    from backend.features.cards import parsing
    from backend.pipeline.context import load_pipeline_context

    extensions = {
        "orb": {
            "fragments": {
                "interactive": [
                    {
                        "id": "card_trim",
                        "label": "Card Trim",
                        "description": "Trim actions.",
                        "field_type": "recast",
                        "gate_question": "Do more than two actions happen?",
                        "gate_replies": 2,
                    }
                ]
            }
        }
    }
    card = await client.post_json("/api/characters", json={"name": "Gated", "extensions": extensions})
    exported = tmp_path / "export.png"
    exported.write_bytes((await client.get(f"/api/characters/{card['id']}/export")).content)
    reimported = parsing.card_to_dict(parsing.parse(str(exported)))
    assert reimported["extensions"]["orb"]["fragments"] == extensions["orb"]["fragments"]

    copy = await client.post_json("/api/characters", json={**reimported, "name": "Gated copy"})
    conv = await client.post_json("/api/conversations", json={"character_card_id": copy["id"]})
    ctx = await load_pipeline_context(conv["id"])
    assert ctx is not None
    fragment = next(row for row in ctx.interactive_fragments if row["id"] == "card_trim")
    assert fragment["gate_question"] == "Do more than two actions happen?"
    assert fragment["gate_replies"] == 2


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


def test_migration_0095_renames_post_processing_seeds_trim_once_and_reruns_safely():
    import importlib
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE interactive_fragments (id TEXT PRIMARY KEY, label TEXT NOT NULL, description TEXT NOT NULL, "
        "field_type TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, "
        "injection_label TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0, "
        "post_processing_gate TEXT NOT NULL DEFAULT '', post_processing_gate_replies INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO interactive_fragments (id, label, description, field_type, injection_label, sort_order, "
        "post_processing_gate, post_processing_gate_replies) VALUES "
        "('tighten', 'Tighten', 'd', 'post_processing', 'Tighten', 3, 'Is it long?', 2), "
        "('mood', 'Mood', 'd', 'string', 'Mood', 5, '', 0)"
    )
    migrate = importlib.import_module("backend.database.migrations.0095_reply_edit_fragments").migrate

    migrate(conn)
    migrate(conn)

    rows = conn.execute(
        "SELECT id, field_type, enabled, sort_order, gate_question, gate_replies FROM interactive_fragments ORDER BY id"
    )
    assert rows.fetchall() == [
        ("mood", "string", 1, 5, "", 0),
        ("tighten", "recast", 1, 3, "Is it long?", 2),
        ("trim_prose", "rewrite", 0, 6, "", 0),
    ]
    conn.close()
