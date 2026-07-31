"""Card-embedded fragments (extensions.orb.fragments) merging into the pipeline.

The merge happens once, in _load_pipeline_context: enabled card fragments join
the global lists for the turn (globals win on id collision), before the
active_moods prune so an active card mood survives. api_get_context_size
applies the same rule for its estimate.
"""

from __future__ import annotations

from backend.database import update_director_state
from backend.pipeline.context import _load_pipeline_context

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
    resp = await client.post(
        "/api/fragments",
        json={"id": "collide_mood", "label": "Global Mood", "description": "d", "prompt_text": "g"},
    )
    assert resp.status_code == 200
    _, cid = await _make_card_conv(client)

    ctx = await _load_pipeline_context(cid)
    assert ctx is not None
    moods = {f["id"]: f for f in ctx.mood_fragments}
    assert "card_mood" in moods
    assert "off_mood" not in moods  # disabled card fragments dropped
    assert moods["collide_mood"]["label"] == "Global Mood"  # global wins on collision

    interactive = {f["id"]: f for f in ctx.interactive_fragments}
    assert interactive["card_trust"]["field_type"] == "progressive"
    assert interactive["card_trust"]["sort_order"] >= 10_000  # sorts after globals


async def test_conversation_without_card_fragments_unaffected(client, db):
    card = (await client.post("/api/characters", json={"name": "Plain"})).json()
    conv = (await client.post("/api/conversations", json={"character_card_id": card["id"]})).json()
    ctx = await _load_pipeline_context(conv["id"])
    assert ctx is not None
    assert not any(f["id"].startswith("card_") for f in ctx.mood_fragments)


async def test_active_card_mood_survives_prune(client, db):
    _, cid = await _make_card_conv(client)
    await update_director_state(cid, ["card_mood", "ghost_mood"])
    ctx = await _load_pipeline_context(cid)
    assert ctx is not None
    # card_mood resolves against the merged list; ghost_mood is pruned as before.
    assert ctx.director["active_moods"] == ["card_mood"]


async def test_context_size_endpoint_handles_card_fragments(client, db):
    _, cid = await _make_card_conv(client)
    resp = await client.get(f"/api/conversations/{cid}/context-size")
    assert resp.status_code == 200
