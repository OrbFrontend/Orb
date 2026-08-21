from __future__ import annotations

import json


async def test_create_character_persists_to_db(client, db):
    payload = {
        "name": "Lira",
        "description": "A wandering bard.",
        "personality": "Cheerful",
        "scenario": "A tavern",
        "tags": ["fantasy", "bard"],
    }
    resp = await client.post("/api/characters", json=payload)
    assert resp.status_code == 200
    card_id = resp.json()["id"]

    async with db.execute("SELECT name, description, tags FROM character_cards WHERE id = ?", (card_id,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["name"] == "Lira"
    assert row["description"] == "A wandering bard."
    assert json.loads(row["tags"]) == ["fantasy", "bard"]


async def test_list_characters_includes_created(client, db):
    await client.post("/api/characters", json={"name": "Rook"})
    resp = await client.get("/api/characters")
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()]
    assert "Rook" in names


async def test_get_character_by_id(client, db):
    create_resp = await client.post("/api/characters", json={"name": "Zara", "description": "A mage."})
    card_id = create_resp.json()["id"]

    resp = await client.get(f"/api/characters/{card_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Zara"
    assert resp.json()["description"] == "A mage."


async def test_get_nonexistent_character_returns_404(client, db):
    resp = await client.get("/api/characters/no-such-id")
    assert resp.status_code == 404


async def test_update_character_persists_to_db(client, db):
    create_resp = await client.post("/api/characters", json={"name": "Old Name", "scenario": "Old scenario"})
    card_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/characters/{card_id}",
        json={"name": "New Name", "scenario": "New scenario"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"

    async with db.execute("SELECT name, scenario FROM character_cards WHERE id = ?", (card_id,)) as cur:
        row = await cur.fetchone()
    assert row["name"] == "New Name"
    assert row["scenario"] == "New scenario"


async def test_update_character_syncs_to_linked_conversations(client, db):
    create_resp = await client.post(
        "/api/characters",
        json={"name": "SyncChar", "scenario": "Original scenario"},
    )
    card_id = create_resp.json()["id"]

    conv_resp = await client.post("/api/conversations", json={"character_card_id": card_id})
    cid = conv_resp.json()["id"]

    await client.put(f"/api/characters/{card_id}", json={"scenario": "Updated scenario"})

    async with db.execute("SELECT character_scenario FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["character_scenario"] == "Updated scenario"


async def test_rename_character_updates_matching_conversation_titles(client, db):
    create_resp = await client.post(
        "/api/characters",
        json={"name": "Old Name", "scenario": "A scenario"},
    )
    card_id = create_resp.json()["id"]

    conv_resp = await client.post("/api/conversations", json={"character_card_id": card_id})
    cid = conv_resp.json()["id"]

    # Title should have defaulted to character name
    async with db.execute("SELECT title FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["title"] == "Old Name"

    await client.put(f"/api/characters/{card_id}", json={"name": "New Name"})

    async with db.execute("SELECT title, character_name FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["title"] == "New Name"
    assert row["character_name"] == "New Name"


async def test_rename_character_leaves_custom_title_alone(client, db):
    create_resp = await client.post(
        "/api/characters",
        json={"name": "Original", "scenario": "A scenario"},
    )
    card_id = create_resp.json()["id"]

    conv_resp = await client.post(
        "/api/conversations",
        json={"character_card_id": card_id, "title": "Custom Title"},
    )
    cid = conv_resp.json()["id"]

    await client.put(f"/api/characters/{card_id}", json={"name": "Renamed"})

    async with db.execute("SELECT title, character_name FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["title"] == "Custom Title"
    assert row["character_name"] == "Renamed"


async def test_delete_character_removes_from_db(client, db):
    create_resp = await client.post("/api/characters", json={"name": "Doomed"})
    card_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/characters/{card_id}")
    assert resp.status_code == 200

    async with db.execute("SELECT id FROM character_cards WHERE id = ?", (card_id,)) as cur:
        row = await cur.fetchone()
    assert row is None


async def test_blank_character_name_returns_422(client, db):
    resp = await client.post("/api/characters", json={"name": "   "})
    assert resp.status_code == 422


async def test_update_blank_name_returns_422(client):
    create_resp = await client.post("/api/characters", json={"name": "Valid"})
    card_id = create_resp.json()["id"]

    resp = await client.put(f"/api/characters/{card_id}", json={"name": "  "})
    assert resp.status_code == 422


async def test_update_nonexistent_character_returns_404(client):
    resp = await client.put("/api/characters/no-such-id", json={"name": "Ghost"})
    assert resp.status_code == 404


async def test_create_character_with_explicit_id(client, db):
    resp = await client.post("/api/characters", json={"id": "my-stable-id", "name": "Stable"})
    assert resp.status_code == 200
    assert resp.json()["id"] == "my-stable-id"

    async with db.execute("SELECT id FROM character_cards WHERE id = 'my-stable-id'") as cur:
        row = await cur.fetchone()
    assert row is not None


async def test_alternate_greetings_stored_as_json(client, db):
    greetings = ["Hello there.", "Good day, stranger."]
    resp = await client.post(
        "/api/characters",
        json={"name": "Greeter", "alternate_greetings": greetings},
    )
    assert resp.status_code == 200
    card_id = resp.json()["id"]

    async with db.execute("SELECT alternate_greetings FROM character_cards WHERE id = ?", (card_id,)) as cur:
        row = await cur.fetchone()
    assert json.loads(row["alternate_greetings"]) == greetings


async def test_delete_character_keeps_conversations_by_default(client, db):
    card_resp = await client.post("/api/characters", json={"name": "Keeper"})
    card_id = card_resp.json()["id"]
    conv_resp = await client.post("/api/conversations", json={"character_card_id": card_id})
    cid = conv_resp.json()["id"]

    await client.delete(f"/api/characters/{card_id}")

    # Conversation must still exist; character_card_id is left as a dangling reference
    async with db.execute("SELECT id FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row is not None


async def test_delete_character_with_delete_conversations_flag(client, db):
    card_resp = await client.post("/api/characters", json={"name": "Purged"})
    card_id = card_resp.json()["id"]
    conv_resp = await client.post("/api/conversations", json={"character_card_id": card_id})
    cid = conv_resp.json()["id"]

    await client.delete(f"/api/characters/{card_id}?delete_conversations=true")

    async with db.execute("SELECT id FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row is None


async def test_update_character_updates_timestamp(client, db):
    create_resp = await client.post("/api/characters", json={"name": "Timestamped"})
    card_id = create_resp.json()["id"]
    original_ts = create_resp.json()["updated_at"]

    import asyncio

    await asyncio.sleep(0.01)

    await client.put(f"/api/characters/{card_id}", json={"description": "Changed."})

    async with db.execute("SELECT updated_at FROM character_cards WHERE id = ?", (card_id,)) as cur:
        row = await cur.fetchone()
    assert row["updated_at"] > original_ts


async def test_update_character_tags_persists_to_db(client, db):
    create_resp = await client.post(
        "/api/characters",
        json={"name": "Tagged", "tags": ["original"]},
    )
    card_id = create_resp.json()["id"]

    resp = await client.put(f"/api/characters/{card_id}", json={"tags": ["action", "drama"]})
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["action", "drama"]

    async with db.execute("SELECT tags FROM character_cards WHERE id = ?", (card_id,)) as cur:
        row = await cur.fetchone()
    assert json.loads(row["tags"]) == ["action", "drama"]


# 1x1 transparent PNG
_PNG_1x1_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


async def test_avatar_response_is_cacheable_with_conditional_get(client, db):
    create = await client.post(
        "/api/characters",
        json={"name": "Avatared", "avatar_b64": _PNG_1x1_B64, "avatar_mime": "image/png"},
    )
    card_id = create.json()["id"]

    resp = await client.get(f"/api/characters/{card_id}/avatar")
    assert resp.status_code == 200
    # The global no-cache middleware must NOT clobber the avatar's cache headers.
    cache_control = resp.headers["cache-control"]
    assert "no-store" not in cache_control
    assert "max-age" in cache_control
    etag = resp.headers["etag"]
    assert etag

    # A matching If-None-Match yields a bodyless 304 (cheap revalidation).
    resp304 = await client.get(f"/api/characters/{card_id}/avatar", headers={"If-None-Match": etag})
    assert resp304.status_code == 304
    assert resp304.content == b""

    # Non-avatar API responses still default to no-store.
    settings = await client.get("/api/settings")
    assert settings.headers["cache-control"] == "no-store"


async def test_list_characters_omits_heavy_text_fields(client, db):
    await client.post(
        "/api/characters",
        json={"name": "Heavy", "description": "x" * 100, "first_mes": "y" * 100, "scenario": "z" * 100},
    )
    resp = await client.get("/api/characters")
    assert resp.status_code == 200
    card = next(c for c in resp.json() if c["name"] == "Heavy")
    # The list projection drops the large text bodies (lazy-loaded per card on edit).
    for heavy in ("description", "personality", "scenario", "first_mes", "system_prompt"):
        assert heavy not in card
    # ...but keeps the lightweight fields the sidebar/grid render.
    assert card["has_avatar"] is False
    assert "tags" in card


async def test_list_characters_reports_card_weight_without_the_bodies(client, db):
    # New Group Chat's context-mode recommendation weighs the chosen cast, and
    # the library list is the only card payload creation holds. `def_chars` is
    # how it gets the measure without reopening the decision above.
    await client.post(
        "/api/characters",
        json={
            "name": "Weighed",
            "description": "d" * 400,
            "personality": "p" * 200,
            "mes_example": "e" * 300,
            # Excluded on purpose: every context mode keeps post-history in the
            # speaker's trailing message, so it cannot discriminate between them.
            "post_history_instructions": "h" * 500,
            # Not card identity text either — neither mode puts these anywhere
            # the other doesn't.
            "scenario": "s" * 500,
            "first_mes": "f" * 500,
        },
    )
    resp = await client.get("/api/characters")
    card = next(c for c in resp.json() if c["name"] == "Weighed")
    assert card["def_chars"] == 900
    assert "description" not in card

    # A card with no text at all weighs nothing rather than going missing — the
    # client reads 0 as "nothing here worth caching".
    await client.post("/api/characters", json={"name": "Bare"})
    resp = await client.get("/api/characters")
    assert next(c for c in resp.json() if c["name"] == "Bare")["def_chars"] == 0


async def test_post_history_instructions_synced_on_update(client, db):
    card_resp = await client.post(
        "/api/characters",
        json={"name": "PostHist", "post_history_instructions": "Original instructions"},
    )
    card_id = card_resp.json()["id"]
    conv_resp = await client.post("/api/conversations", json={"character_card_id": card_id})
    cid = conv_resp.json()["id"]

    await client.put(
        f"/api/characters/{card_id}",
        json={"post_history_instructions": "New instructions"},
    )

    async with db.execute("SELECT post_history_instructions FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["post_history_instructions"] == "New instructions"


async def test_extensions_round_trip(client, db, tmp_path):
    """extensions persists through create → get → unrelated update → PNG export,
    with third-party keys carried verbatim alongside orb.fragments."""
    ext = {
        "acme_ext": {"nested": [1, 2]},
        "orb": {
            "fragments": {
                "mood": [
                    {
                        "id": "dreamy",
                        "label": "Dreamy",
                        "description": "",
                        "prompt_text": "drift",
                        "negative_prompt": "",
                        "enabled": True,
                    }
                ],
                "interactive": [],
            }
        },
    }
    resp = await client.post("/api/characters", json={"name": "ExtChar", "extensions": ext})
    assert resp.status_code == 200
    card_id = resp.json()["id"]

    got = (await client.get(f"/api/characters/{card_id}")).json()
    assert got["extensions"] == ext

    # An update that doesn't send extensions leaves them untouched.
    await client.put(f"/api/characters/{card_id}", json={"scenario": "new"})
    got = (await client.get(f"/api/characters/{card_id}")).json()
    assert got["extensions"] == ext

    # Export embeds the dict in the V2 chara chunk; a re-parse recovers it.
    export = await client.get(f"/api/characters/{card_id}/export")
    assert export.status_code == 200
    png = tmp_path / "card.png"
    png.write_bytes(export.content)
    from backend.features.cards import parsing as tavern_cards

    parsed = tavern_cards.parse(str(png))
    assert parsed.data.extensions["acme_ext"] == {"nested": [1, 2]}
    assert parsed.data.extensions["orb"]["fragments"]["mood"][0]["id"] == "dreamy"


async def test_extensions_absent_decodes_to_empty_dict(client, db):
    resp = await client.post("/api/characters", json={"name": "NoExt"})
    card_id = resp.json()["id"]
    got = (await client.get(f"/api/characters/{card_id}")).json()
    assert got["extensions"] == {}
    # Pre-migration rows have a NULL column, which must decode the same way.
    await db.execute("UPDATE character_cards SET extensions = NULL WHERE id = ?", (card_id,))
    await db.commit()
    got = (await client.get(f"/api/characters/{card_id}")).json()
    assert got["extensions"] == {}


def _profile_call(**arguments) -> dict:
    """The forced ``draft_public_profile`` response shape.

    ``_llm_mock._pass_from_tool_choice`` routes any forced tool name it does not
    recognise as a core pass tool to the ``workflow`` queue, and this schema is
    deliberately not in ``inference.tool_registry.TOOLS`` — so this is the queue
    the public-profile drafter reads from.
    """
    return {"tool_calls": [{"type": "function", "function": {"name": "draft_public_profile", "arguments": arguments}}]}


async def test_public_profile_generate_returns_the_tool_call_fields(client, db, llm_mock):
    """The wire shape of the card drafter: the model's two fields, stripped.

    Nothing is persisted — generation hands back an editable draft and the card
    only changes when `PUT …/public-profile` saves it.
    """
    card_id = (
        await client.post(
            "/api/characters",
            json={"name": "Lira", "description": "A wandering bard.", "personality": "Cheerful"},
        )
    ).json()["id"]
    llm_mock.enqueue_workflow(_profile_call(appearance="  A bard in road-worn green.  ", role="\nTavern regular\n"))

    resp = await client.post(f"/api/characters/{card_id}/public-profile/generate")
    assert resp.status_code == 200
    assert resp.json() == {"appearance": "A bard in road-worn green.", "role": "Tavern regular"}

    card = (await client.get(f"/api/characters/{card_id}")).json()
    assert (card.get("extensions") or {}).get("orb", {}).get("public_profile") is None

    # The card's own text is what the model was asked to summarize.
    sent = llm_mock.captured[-1]["messages"][-1]["content"]
    assert "A wandering bard." in sent and "Cheerful" in sent


async def test_public_profile_generate_raises_when_the_model_returns_no_call(client, db, llm_mock):
    """No silent degrade. A draft assembled from the card's first line under a
    "Draft ready" toast is indistinguishable from a real answer, and the same
    code path now runs in a loop that writes N overrides the user saves at once."""
    card_id = (await client.post("/api/characters", json={"name": "Lira", "description": "A bard."})).json()["id"]
    llm_mock.enqueue_workflow({"role": "assistant", "content": "Sorry, I can't do that."})

    resp = await client.post(f"/api/characters/{card_id}/public-profile/generate")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "The model did not return a usable profile."

