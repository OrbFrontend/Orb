"""Phase 5: contributed interactive-fragment types across their fixed lifecycle."""

from __future__ import annotations

import json

import backend.database as dbmod
from backend.features.extensions import interpreter
from backend.features.extensions.runtime import current_state
from backend.inference import build_direct_scene_tool
from backend.pipeline import handle_regenerate, handle_turn
from backend.pipeline.fragment_types import (
    reduce_fragment_outputs,
    resolve_fragment_instances,
    with_fragment_priors,
)
from backend.workflows import current_snapshot
from tests.extension_packages import fragment_meter_package

from .conftest import install

METER_CONFIG = {
    "minimum": 0,
    "maximum": 100,
    "initial": 50,
    "max_delta": 10,
}

METER_FRAGMENT = {
    "id": "tension",
    "label": "Tension",
    "description": "How tense the scene is.",
    "field_type": "scene-meter:meter",
    "required": True,
    "enabled": True,
    "injection_label": "Scene tension",
    "sort_order": 5,
    "direction_note_timing": "post_turn",
    "type_config": METER_CONFIG,
}


async def _drain(agen) -> list[dict]:
    return [event async for event in agen]


def _direct_scene(arguments: dict) -> list[dict]:
    return [{"type": "function", "function": {"name": "direct_scene", "arguments": arguments}}]


async def test_meter_catalog_config_roundtrip_and_provider_loss(client, db):
    await install(client, fragment_meter_package())

    catalog = (await client.get("/api/interactive-fragment-types")).json()
    descriptor = next(item for item in catalog["types"] if item["id"] == "scene-meter:meter")
    assert descriptor["kind"] == "contributed"
    assert descriptor["storage"] == "assistant_progressive"
    assert descriptor["config_view"]["root"]["component"] == "card"
    assert descriptor["value_view"]["root"]["component"] == "meter"

    created = await client.post("/api/interactive-fragments", json=METER_FRAGMENT)
    assert created.status_code == 200, created.text
    assert created.json()["type_config"] == METER_CONFIG
    row = await (await db.execute("SELECT field_type, type_config FROM interactive_fragments WHERE id = 'tension'")).fetchone()
    assert row["field_type"] == "scene-meter:meter"
    assert json.loads(row["type_config"]) == METER_CONFIG

    invalid = await client.put(
        "/api/interactive-fragments/tension",
        json={"type_config": {**METER_CONFIG, "max_delta": 0}},
    )
    assert invalid.status_code == 422
    assert "type configuration is invalid" in invalid.json()["detail"]

    await db.execute(
        "UPDATE interactive_fragments SET type_config = ? WHERE id = 'tension'",
        (json.dumps({**METER_CONFIG, "max_delta": 0}),),
    )
    await db.commit()
    diagnosed = next(item for item in (await client.get("/api/interactive-fragments")).json() if item["id"] == "tension")
    assert diagnosed["type_available"] is False
    assert "type configuration is invalid" in diagnosed["type_diagnostic"]
    await db.execute(
        "UPDATE interactive_fragments SET type_config = ? WHERE id = 'tension'",
        (json.dumps(METER_CONFIG),),
    )
    await db.commit()

    changed = await client.post("/api/extensions/scene-meter/enabled", json={"enabled": False})
    assert changed.status_code == 200
    assert current_snapshot().fragment_type("scene-meter:meter") is None

    preserved = next(item for item in (await client.get("/api/interactive-fragments")).json() if item["id"] == "tension")
    assert preserved["field_type"] == "scene-meter:meter"
    assert preserved["type_config"] == METER_CONFIG
    assert preserved["type_available"] is False
    assert "unavailable" in preserved["type_diagnostic"]

    # Missing providers are visible and inert. Editing requires the provider
    # so Orb never persists a shape it can no longer validate.
    rejected = await client.put("/api/interactive-fragments/tension", json={"label": "Still tension"})
    assert rejected.status_code == 409


async def test_meter_schema_reducer_carry_forward_and_writer_context(client):
    await install(client, fragment_meter_package())
    snapshot = current_snapshot()
    resolution = resolve_fragment_instances(snapshot, [METER_FRAGMENT])
    assert resolution.diagnostics == ()

    # The resolved schema is captured once and stable for the whole turn.
    first_schema = build_direct_scene_tool(resolution.fragments)
    second_schema = build_direct_scene_tool(resolution.fragments)
    assert first_schema == second_schema
    assert '"initial"' not in json.dumps(first_schema)
    assert "currently 50" not in json.dumps(first_schema)
    delta = first_schema["function"]["parameters"]["properties"]["tension"]["properties"]["delta"]
    assert delta["minimum"] == -10
    assert delta["maximum"] == 10

    prepared, prior_diagnostics = with_fragment_priors(resolution.fragments, {})
    assert prior_diagnostics == ()
    assert prepared[0]["_previous_value"] == {"value": 50}
    assert "currently 50" in prepared[0]["prior_context"]

    reduced = await reduce_fragment_outputs(
        prepared,
        {"tension": {"delta": 8, "reason": "The characters are cornered."}},
        is_cancelled=lambda: False,
    )
    assert reduced.diagnostics == ()
    assert reduced.fields["tension"] == {
        "value": 58,
        "reason": "The characters are cornered.",
    }
    assert reduced.progressive_fields == reduced.fields
    assert "50 → 58" in reduced.rendered_fragments[0]["writer_context"]

    near_limit = {
        **METER_FRAGMENT,
        "id": "near_limit",
        "type_config": {**METER_CONFIG, "initial": 95},
    }
    bounded = resolve_fragment_instances(snapshot, [near_limit]).fragments
    bounded, _ = with_fragment_priors(bounded, {})
    clamped = await reduce_fragment_outputs(
        bounded,
        {"near_limit": {"delta": 10, "reason": "The tension spikes."}},
        is_cancelled=lambda: False,
    )
    assert clamped.progressive_fields["near_limit"]["value"] == 100

    # Invalid or absent Director output carries the persisted value forward.
    next_turn, _ = with_fragment_priors(resolution.fragments, reduced.progressive_fields)
    carried = await reduce_fragment_outputs(
        next_turn,
        {"tension": {"delta": 11, "reason": "Outside the configured bound."}},
        is_cancelled=lambda: False,
    )
    assert carried.fields == reduced.fields
    assert carried.progressive_fields == reduced.progressive_fields
    assert "Director value was invalid" in carried.diagnostics[0]["message"]


async def test_permission_revocation_removes_schema_and_stops_captured_reducer(client):
    await install(client, fragment_meter_package())
    snapshot = current_snapshot()
    resolved = resolve_fragment_instances(snapshot, [METER_FRAGMENT]).fragments
    prepared, _ = with_fragment_priors(resolved, {})

    revoked = await client.put("/api/extensions/scene-meter/permissions", json={"permissions": []})
    assert revoked.status_code == 200
    assert current_snapshot().fragment_type("scene-meter:meter") is None

    # A captured descriptor cannot use stale consent. The adapter re-checks
    # the live grant immediately before executing the reducer.
    result = await reduce_fragment_outputs(
        prepared,
        {"tension": {"delta": 2, "reason": "Would otherwise advance."}},
        is_cancelled=lambda: False,
    )
    assert result.fields["tension"] == {"value": 50}
    assert "reducer failed" in result.diagnostics[0]["message"]


async def test_unexecutable_reducer_is_diagnosed_and_not_published(client, monkeypatch):
    monkeypatch.setattr(interpreter, "UNIMPLEMENTED_OPS", frozenset({"math.add"}))
    await install(client, fragment_meter_package())

    assert current_snapshot().fragment_type("scene-meter:meter") is None
    entry = current_state().get("scene-meter")
    assert entry is not None
    assert entry.blocked == ("fragment type contributions",)
    assert "fragment type contributions" in entry.diagnostic


async def test_meter_runs_through_turn_persistence_and_branch_regeneration(client, llm_mock):
    await install(client, fragment_meter_package())
    created = await client.post("/api/interactive-fragments", json=METER_FRAGMENT)
    assert created.status_code == 200, created.text
    await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})

    cid = "conv-fragment-meter"
    await dbmod.create_conversation(cid, "meter", "Bot", "A tense scene.")

    llm_mock.enqueue_director(_direct_scene({"moods": [], "tension": {"delta": 8, "reason": "The characters are cornered."}}))
    llm_mock.enqueue_writer("First reply.")
    first_events = await _drain(handle_turn(cid, "Begin."))
    first_done = next(event["data"] for event in first_events if event["event"] == "director_done")
    assert first_done["director_fields"]["tension"]["delta"] == 8
    assert first_done["progressive_fields"]["tension"]["value"] == 58
    assert "50 → 58" in first_done["injection_block"]

    first_messages = await dbmod.get_messages(cid)
    first_assistant = next(message for message in first_messages if message["role"] == "assistant")
    assert first_assistant["progressive_fields"]["tension"]["value"] == 58
    first_writer = next(call for call in llm_mock.captured if call["pass"] == "writer")
    assert "50 → 58" in json.dumps(first_writer["messages"], ensure_ascii=False)

    llm_mock.enqueue_director(_direct_scene({"moods": [], "tension": {"delta": -3, "reason": "An escape route appears."}}))
    llm_mock.enqueue_writer("Second reply.")
    second_events = await _drain(handle_turn(cid, "Continue."))
    second_done = next(event["data"] for event in second_events if event["event"] == "director_done")
    assert second_done["progressive_fields"]["tension"]["value"] == 55
    assert "58 → 55" in second_done["injection_block"]

    second_messages = await dbmod.get_messages(cid)
    second_assistant = second_messages[-1]
    assert second_assistant["role"] == "assistant"
    assert second_assistant["progressive_fields"]["tension"]["value"] == 55

    # Regeneration must rewind to the previous assistant on this branch (58),
    # not reuse the superseded turn's latest process-wide Director state (55).
    llm_mock.enqueue_director(_direct_scene({"moods": [], "tension": {"delta": 1, "reason": "The danger returns."}}))
    llm_mock.enqueue_writer("Regenerated second reply.")
    regen_events = await _drain(handle_regenerate(cid, second_assistant["id"]))
    regen_done = next(event["data"] for event in regen_events if event["event"] == "director_done")
    assert regen_done["progressive_fields"]["tension"]["value"] == 59
    assert "58 → 59" in regen_done["injection_block"]

    regenerated_path = await dbmod.get_messages(cid)
    assert regenerated_path[-1]["content"] == "Regenerated second reply."
    assert regenerated_path[-1]["progressive_fields"]["tension"]["value"] == 59
    assert regenerated_path[-1]["parent_id"] == second_assistant["parent_id"]

    # A turn while the provider is unavailable keeps the opaque branch value
    # inert: it is absent from the Director schema and Writer context, but it is
    # not erased from the assistant node. The diagnostic is persisted for a
    # historical Inspector revisit rather than existing only in the live SSE.
    disabled = await client.post("/api/extensions/scene-meter/enabled", json={"enabled": False})
    assert disabled.status_code == 200
    # A registry-generation change intentionally replaces the next turn's tool
    # blob; isolate each generation so the fixture's within-generation KV check
    # does not mistake that explicit invalidation for cross-pass drift.
    llm_mock.captured.clear()
    llm_mock.enqueue_director(_direct_scene({"moods": []}))
    llm_mock.enqueue_writer("Providerless reply.")
    providerless_events = await _drain(handle_turn(cid, "Continue without the extension."))
    providerless_done = next(event["data"] for event in providerless_events if event["event"] == "director_done")
    assert providerless_done["progressive_fields"]["tension"]["value"] == 59
    assert "unavailable" in providerless_done["fragment_diagnostics"][0]["message"]

    providerless_path = await dbmod.get_messages(cid)
    providerless_assistant = providerless_path[-1]
    assert providerless_assistant["progressive_fields"]["tension"]["value"] == 59
    historical = await client.get(f"/api/conversations/{cid}/messages/{providerless_assistant['id']}/director-log")
    assert historical.status_code == 200
    assert historical.json()["fragment_diagnostics"] == providerless_done["fragment_diagnostics"]

    enabled = await client.post("/api/extensions/scene-meter/enabled", json={"enabled": True})
    assert enabled.status_code == 200
    llm_mock.captured.clear()
    llm_mock.enqueue_director(_direct_scene({"moods": [], "tension": {"delta": 2, "reason": "The threat returns."}}))
    llm_mock.enqueue_writer("Restored reply.")
    restored_events = await _drain(handle_turn(cid, "Restore it."))
    restored_done = next(event["data"] for event in restored_events if event["event"] == "director_done")
    assert restored_done["progressive_fields"]["tension"]["value"] == 61
