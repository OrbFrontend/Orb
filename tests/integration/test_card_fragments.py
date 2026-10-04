"""Card-embedded fragments (extensions.orb.fragments) merging into the pipeline.

The merge happens once, in load_pipeline_context: enabled card fragments join the global lists for the turn (globals win on id
collision), before the active_moods prune so an active card mood survives. api_get_context_size applies the same rule for its
estimate.
"""

from __future__ import annotations

from backend.database import update_director_state
from backend.pipeline.context import load_pipeline_context

EXT = {
    "orb": {
        "fragments": {
            "mood": [
                {"id": "card_mood", "label": "Card Mood", "description": "d", "prompt_text": "p", "negative_prompt": ""},
                {"id": "collide_mood", "label": "Hijack Attempt", "description": "d", "prompt_text": "p"},
                {"id": "off_mood", "label": "Off", "prompt_text": "p", "enabled": False},
            ],
            "interactive": [
                {
                    "id": "card_trust",
                    "label": "Trust",
                    "description": "d",
                    "field_type": "progressive",
                    "injection_label": "Trust level",
                }
            ],
        }
    }
}


async def _make_card_conv(client, ext=EXT):
    card = (await client.post("/api/characters", json={"name": "FragChar", "extensions": ext})).json()
    conv = (await client.post("/api/conversations", json={"character_card_id": card["id"]})).json()
    return card["id"], conv["id"]


async def test_card_fragments_merge_into_pipeline_context(client, db):
    await client.post_checked(
        "/api/fragments", json={"id": "collide_mood", "label": "Global Mood", "description": "d", "prompt_text": "g"}
    )
    _, cid = await _make_card_conv(client)

    ctx = await load_pipeline_context(cid)
    assert ctx is not None
    moods = {f["id"]: f for f in ctx.mood_fragments}
    assert "card_mood" in moods
    assert "off_mood" not in moods  # disabled card fragments dropped
    assert moods["collide_mood"]["label"] == "Global Mood"  # global wins on collision

    interactive = {f["id"]: f for f in ctx.interactive_fragments}
    # Shared cards keep the legacy type; it is read as the state fragment it converts to.
    assert interactive["card_trust"]["field_type"] == "state"
    assert interactive["card_trust"]["state_mode"] == "value"
    assert interactive["card_trust"]["state_update"] == "before_writer"
    assert interactive["card_trust"]["sort_order"] >= 10_000  # sorts after globals
    assert [fragment.id for fragment in ctx.state_contract.director_values()] == ["card_trust"]


async def test_card_row_shadowing_a_disabled_global_keeps_the_blob_slot(client, db):
    # The enabled list is the blob's order filtered, so a card row standing in
    # for a disabled global is listed where the shared schemas offer it.
    for body in (
        {
            "id": "card_trust",
            "label": "Twin",
            "description": "g",
            "injection_label": "Twin",
            "sort_order": -100,
            "enabled": False,
        },
        {"id": "after_twin", "label": "After", "description": "g", "injection_label": "After", "sort_order": -50},
    ):
        assert (await client.post("/api/interactive-fragments", json=body)).status_code == 200
    _, cid = await _make_card_conv(client)

    ctx = await load_pipeline_context(cid)
    assert ctx is not None and ctx.defined_fragments is not None
    enabled_ids = [f["id"] for f in ctx.interactive_fragments]
    assert enabled_ids.index("card_trust") < enabled_ids.index("after_twin")
    assert enabled_ids == [f["id"] for f in ctx.defined_fragments if f["id"] in enabled_ids]
    assert next(f for f in ctx.interactive_fragments if f["id"] == "card_trust")["field_type"] == "state"


async def test_conversation_without_card_fragments_unaffected(client, db):
    card = (await client.post("/api/characters", json={"name": "Plain"})).json()
    conv = (await client.post("/api/conversations", json={"character_card_id": card["id"]})).json()
    ctx = await load_pipeline_context(conv["id"])
    assert ctx is not None
    assert not any(f["id"].startswith("card_") for f in ctx.mood_fragments)


async def test_active_card_mood_survives_prune(client, db):
    _, cid = await _make_card_conv(client)
    await update_director_state(cid, ["card_mood", "ghost_mood"])
    ctx = await load_pipeline_context(cid)
    assert ctx is not None
    # card_mood resolves against the merged list; ghost_mood is pruned as before.
    assert ctx.director["active_moods"] == ["card_mood"]


async def test_context_size_endpoint_handles_card_fragments(client, db):
    _, cid = await _make_card_conv(client)
    await client.get_checked(f"/api/conversations/{cid}/context-size")
