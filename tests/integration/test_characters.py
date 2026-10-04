from __future__ import annotations

import base64
import io
import json
import zipfile

from PIL import Image


async def test_create_character_persists_to_db(client, db):
    payload = {
        "name": "Lira",
        "description": "A wandering bard.",
        "personality": "Cheerful",
        "scenario": "A tavern",
        "tags": ["fantasy", "bard"],
    }
    card_id = await client.create("/api/characters", json=payload)

    row = await db.one("SELECT name, description, tags FROM character_cards WHERE id = ?", (card_id,))
    assert row is not None
    assert row["name"] == "Lira"
    assert row["description"] == "A wandering bard."
    assert json.loads(row["tags"]) == ["fantasy", "bard"]


async def test_list_characters_includes_created(client, db):
    await client.post("/api/characters", json={"name": "Rook"})
    resp = await client.get_json("/api/characters")
    names = [c["name"] for c in resp]
    assert "Rook" in names


async def test_get_character_by_id(client, db):
    card_id = await client.create("/api/characters", json={"name": "Zara", "description": "A mage."})

    resp = await client.get_json(f"/api/characters/{card_id}")
    assert resp["name"] == "Zara"
    assert resp["description"] == "A mage."


async def test_get_nonexistent_character_returns_404(client, db):
    await client.get_checked("/api/characters/no-such-id", expected_status=404)


async def test_update_character_persists_to_db(client, db):
    card_id = await client.create("/api/characters", json={"name": "Old Name", "scenario": "Old scenario"})

    resp = await client.put_json(f"/api/characters/{card_id}", json={"name": "New Name", "scenario": "New scenario"})
    assert resp["name"] == "New Name"

    row = await db.one("SELECT name, scenario FROM character_cards WHERE id = ?", (card_id,))
    assert row["name"] == "New Name"
    assert row["scenario"] == "New scenario"


async def test_update_character_syncs_to_linked_conversations(client, db):
    card_id = await client.create("/api/characters", json={"name": "SyncChar", "scenario": "Original scenario"})

    cid = await client.create("/api/conversations", json={"character_card_id": card_id})

    await client.put(f"/api/characters/{card_id}", json={"scenario": "Updated scenario"})

    row = await db.one("SELECT character_scenario FROM conversations WHERE id = ?", (cid,))
    assert row["character_scenario"] == "Updated scenario"


async def test_rename_character_updates_matching_conversation_titles(client, db):
    card_id = await client.create("/api/characters", json={"name": "Old Name", "scenario": "A scenario"})

    cid = await client.create("/api/conversations", json={"character_card_id": card_id})

    # Title should have defaulted to character name
    row = await db.one("SELECT title FROM conversations WHERE id = ?", (cid,))
    assert row["title"] == "Old Name"

    await client.put(f"/api/characters/{card_id}", json={"name": "New Name"})

    row = await db.one("SELECT title, character_name FROM conversations WHERE id = ?", (cid,))
    assert row["title"] == "New Name"
    assert row["character_name"] == "New Name"


async def test_rename_character_leaves_custom_title_alone(client, db):
    card_id = await client.create("/api/characters", json={"name": "Original", "scenario": "A scenario"})

    cid = await client.create("/api/conversations", json={"character_card_id": card_id, "title": "Custom Title"})

    await client.put(f"/api/characters/{card_id}", json={"name": "Renamed"})

    row = await db.one("SELECT title, character_name FROM conversations WHERE id = ?", (cid,))
    assert row["title"] == "Custom Title"
    assert row["character_name"] == "Renamed"


async def test_delete_character_removes_from_db(client, db):
    card_id = await client.create("/api/characters", json={"name": "Doomed"})

    await client.delete_checked(f"/api/characters/{card_id}")

    row = await db.one("SELECT id FROM character_cards WHERE id = ?", (card_id,))
    assert row is None


async def test_blank_character_name_returns_422(client, db):
    await client.post_checked("/api/characters", json={"name": "   "}, expected_status=422)


async def test_update_blank_name_returns_422(client):
    card_id = await client.create("/api/characters", json={"name": "Valid"})

    await client.put_checked(f"/api/characters/{card_id}", json={"name": "  "}, expected_status=422)


async def test_update_nonexistent_character_returns_404(client):
    await client.put_checked("/api/characters/no-such-id", json={"name": "Ghost"}, expected_status=404)


async def test_create_character_with_explicit_id(client, db):
    resp = await client.post_json("/api/characters", json={"id": "my-stable-id", "name": "Stable"})
    assert resp["id"] == "my-stable-id"

    row = await db.one("SELECT id FROM character_cards WHERE id = 'my-stable-id'")
    assert row is not None


async def test_alternate_greetings_stored_as_json(client, db):
    greetings = ["Hello there.", "Good day, stranger."]
    card_id = await client.create("/api/characters", json={"name": "Greeter", "alternate_greetings": greetings})

    row = await db.one("SELECT alternate_greetings FROM character_cards WHERE id = ?", (card_id,))
    assert json.loads(row["alternate_greetings"]) == greetings


async def test_delete_character_keeps_conversations_by_default(client, db):
    card_id = await client.create("/api/characters", json={"name": "Keeper"})
    cid = await client.create("/api/conversations", json={"character_card_id": card_id})

    await client.delete(f"/api/characters/{card_id}")

    # Conversation must still exist; character_card_id is left as a dangling reference
    row = await db.one("SELECT id FROM conversations WHERE id = ?", (cid,))
    assert row is not None


async def test_delete_character_with_delete_conversations_flag(client, db):
    card_id = await client.create("/api/characters", json={"name": "Purged"})
    cid = await client.create("/api/conversations", json={"character_card_id": card_id})

    await client.delete(f"/api/characters/{card_id}?delete_conversations=true")

    row = await db.one("SELECT id FROM conversations WHERE id = ?", (cid,))
    assert row is None


async def test_update_character_updates_timestamp(client, db):
    create_resp = await client.post("/api/characters", json={"name": "Timestamped"})
    card_id = create_resp.json()["id"]
    original_ts = create_resp.json()["updated_at"]

    import asyncio

    await asyncio.sleep(0.01)

    await client.put(f"/api/characters/{card_id}", json={"description": "Changed."})

    row = await db.one("SELECT updated_at FROM character_cards WHERE id = ?", (card_id,))
    assert row["updated_at"] > original_ts


async def test_update_character_tags_persists_to_db(client, db):
    card_id = await client.create("/api/characters", json={"name": "Tagged", "tags": ["original"]})

    resp = await client.put_json(f"/api/characters/{card_id}", json={"tags": ["action", "drama"]})
    assert resp["tags"] == ["action", "drama"]

    row = await db.one("SELECT tags FROM character_cards WHERE id = ?", (card_id,))
    assert json.loads(row["tags"]) == ["action", "drama"]


# 1x1 transparent PNG
_PNG_1x1_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


async def test_avatar_response_is_cacheable_with_conditional_get(client, db):
    card_id = await client.create(
        "/api/characters", json={"name": "Avatared", "avatar_b64": _PNG_1x1_B64, "avatar_mime": "image/png"}
    )

    resp = await client.get_checked(f"/api/characters/{card_id}/avatar")
    # The global no-cache middleware must NOT clobber the avatar's cache headers.
    cache_control = resp.headers["cache-control"]
    assert "no-store" not in cache_control
    assert "max-age" in cache_control
    etag = resp.headers["etag"]
    assert etag

    # A matching If-None-Match yields a bodyless 304 (cheap revalidation).
    resp304 = await client.get_checked(
        f"/api/characters/{card_id}/avatar", headers={"If-None-Match": etag}, expected_status=304
    )
    assert resp304.content == b""

    # Non-avatar API responses still default to no-store.
    settings = await client.get("/api/settings")
    assert settings.headers["cache-control"] == "no-store"


def _png_b64(width: int, height: int) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 40, 40)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def test_avatar_thumbnail_is_small_square_and_versioned_by_the_card(client, db):
    card_id = await client.create(
        "/api/characters", json={"name": "Tall", "avatar_b64": _png_b64(600, 900), "avatar_mime": "image/png"}
    )

    resp = await client.get_checked(f"/api/characters/{card_id}/avatar/thumb")
    assert resp.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(resp.content)).size == (192, 192)
    etag = resp.headers["etag"]
    assert (await client.get(f"/api/characters/{card_id}/avatar/thumb", headers={"If-None-Match": etag})).status_code == 304

    # A new avatar must not revalidate against the old thumbnail.
    await client.put(f"/api/characters/{card_id}", json={"avatar_b64": _png_b64(900, 600), "avatar_mime": "image/png"})
    changed = await client.get_checked(f"/api/characters/{card_id}/avatar/thumb", headers={"If-None-Match": etag})
    assert changed.headers["etag"] != etag

    # A source smaller than its thumbnail is served as it is.
    tiny = await client.post("/api/characters", json={"name": "Tiny", "avatar_b64": _PNG_1x1_B64, "avatar_mime": "image/png"})
    tiny_thumb = await client.get(f"/api/characters/{tiny.json()['id']}/avatar/thumb")
    assert tiny_thumb.headers["content-type"] == "image/png"
    assert tiny_thumb.content == base64.b64decode(_PNG_1x1_B64)

    bare = await client.post("/api/characters", json={"name": "Bare"})
    assert (await client.get(f"/api/characters/{bare.json()['id']}/avatar/thumb")).status_code == 404


async def test_list_characters_omits_heavy_text_fields(client, db):
    await client.post(
        "/api/characters", json={"name": "Heavy", "description": "x" * 100, "first_mes": "y" * 100, "scenario": "z" * 100}
    )
    resp = await client.get_json("/api/characters")
    card = next(c for c in resp if c["name"] == "Heavy")
    # The list projection drops the large text bodies (lazy-loaded per card on edit).
    for heavy in ("description", "personality", "scenario", "first_mes", "system_prompt"):
        assert heavy not in card
    # ...but keeps the lightweight fields the sidebar/grid render.
    assert card["has_avatar"] is False
    assert "tags" in card


async def test_list_characters_reports_card_weight_without_the_bodies(client, db):
    # New Group Chat's context-mode recommendation weighs the chosen cast, and the library list is the only card payload
    # creation holds. `def_chars` is how it gets the measure without reopening the decision above.
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
            # Not card identity text either -- neither mode puts these anywhere the other doesn't.
            "scenario": "s" * 500,
            "first_mes": "f" * 500,
        },
    )
    resp = await client.get("/api/characters")
    card = next(c for c in resp.json() if c["name"] == "Weighed")
    assert card["def_chars"] == 900
    assert "description" not in card

    # A card with no text at all weighs nothing rather than going missing -- the client reads 0 as "nothing here worth caching".
    await client.post("/api/characters", json={"name": "Bare"})
    resp = await client.get("/api/characters")
    assert next(c for c in resp.json() if c["name"] == "Bare")["def_chars"] == 0


async def test_post_history_instructions_synced_on_update(client, db):
    card_id = await client.create(
        "/api/characters", json={"name": "PostHist", "post_history_instructions": "Original instructions"}
    )
    cid = await client.create("/api/conversations", json={"character_card_id": card_id})

    await client.put(f"/api/characters/{card_id}", json={"post_history_instructions": "New instructions"})

    row = await db.one("SELECT post_history_instructions FROM conversations WHERE id = ?", (cid,))
    assert row["post_history_instructions"] == "New instructions"


async def test_extensions_round_trip(client, db, tmp_path):
    """extensions persists through create -> get -> unrelated update -> PNG export,
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
    card_id = await client.create("/api/characters", json={"name": "ExtChar", "extensions": ext})

    got = (await client.get(f"/api/characters/{card_id}")).json()
    assert got["extensions"] == ext

    # An update that doesn't send extensions leaves them untouched.
    await client.put(f"/api/characters/{card_id}", json={"scenario": "new"})
    got = (await client.get(f"/api/characters/{card_id}")).json()
    assert got["extensions"] == ext

    # Export embeds the dict in the V2 chara chunk; a re-parse recovers it.
    export = await client.get_checked(f"/api/characters/{card_id}/export")
    png = tmp_path / "card.png"
    png.write_bytes(export.content)
    from backend.features.cards import parsing as tavern_cards

    parsed = tavern_cards.parse(str(png))
    assert parsed.data.extensions["acme_ext"] == {"nested": [1, 2]}
    assert parsed.data.extensions["orb"]["fragments"]["mood"][0]["id"] == "dreamy"


async def test_extensions_absent_decodes_to_empty_dict(client, db):
    card_id = await client.create("/api/characters", json={"name": "NoExt"})
    got = (await client.get(f"/api/characters/{card_id}")).json()
    assert got["extensions"] == {}
    # Pre-migration rows have a NULL column, which must decode the same way.
    await db.execute("UPDATE character_cards SET extensions = NULL WHERE id = ?", (card_id,))
    await db.commit()
    got = (await client.get(f"/api/characters/{card_id}")).json()
    assert got["extensions"] == {}


def _profile_call(**arguments) -> dict:
    """The forced ``draft_public_profile`` response shape.

    ``_llm_mock._pass_from_tool_choice`` routes any forced tool name it does not recognise as a core pass tool to the
    ``workflow`` queue, and this schema is deliberately not in ``prompting.tool_catalog.TOOLS`` -- so this is the queue the
    public-profile drafter reads from.
    """
    return {"tool_calls": [{"type": "function", "function": {"name": "draft_public_profile", "arguments": arguments}}]}


async def test_public_profile_generate_returns_the_tool_call_fields(client, db, llm_mock):
    """The wire shape of the card drafter: the model's two fields, stripped.

    Nothing is persisted -- generation hands back an editable draft and the card
    only changes when `PUT .../public-profile` saves it.
    """
    card_id = await client.create(
        "/api/characters", json={"name": "Lira", "description": "A wandering bard.", "personality": "Cheerful"}
    )
    llm_mock.enqueue_workflow(_profile_call(appearance="  A bard in road-worn green.  ", role="\nTavern regular\n"))

    resp = await client.post_json(f"/api/characters/{card_id}/public-profile/generate")
    assert resp == {"appearance": "A bard in road-worn green.", "role": "Tavern regular"}

    card = (await client.get(f"/api/characters/{card_id}")).json()
    assert (card.get("extensions") or {}).get("orb", {}).get("public_profile") is None

    # The card's own text is what the model was asked to summarize.
    sent = llm_mock.captured[-1]["messages"][-1]["content"]
    assert "A wandering bard." in sent and "Cheerful" in sent


async def test_public_profile_generate_raises_when_the_model_returns_no_call(client, db, llm_mock):
    """No silent degrade. A draft assembled from the card's first line under a
    "Draft ready" toast is indistinguishable from a real answer, and the same
    code path now runs in a loop that writes N overrides the user saves at once."""
    card_id = await client.create("/api/characters", json={"name": "Lira", "description": "A bard."})
    llm_mock.enqueue_workflow({"role": "assistant", "content": "Sorry, I can't do that."})

    resp = await client.post_json(f"/api/characters/{card_id}/public-profile/generate", expected_status=502)
    assert resp["detail"] == "The model did not return a usable profile."


async def test_empty_dynamic_lorebook_survives_card_export_reimport(client, db, tmp_path):
    """A linked Dynamic World with no entries must come back on re-import.

    An empty Dynamic World is the normal starting state -- the Agent writes its lore during play -- so the import path cannot
    use "has entries" as its test for whether the card carries a lorebook worth restoring.
    """
    world = (await client.post("/api/worlds", json={"name": "Things learned about the user"})).json()
    await client.put(f"/api/worlds/{world['id']}/dynamic", json={"enabled": True})
    card_id = await client.create("/api/characters", json={"name": "Assistant", "world_id": world["id"]})

    export = await client.get_checked(f"/api/characters/{card_id}/export")
    png = tmp_path / "assistant.png"
    png.write_bytes(export.content)

    # Re-import into a database that has never seen the World, the way another
    # install would: drop the card, then the (now unlinked) World.
    await client.delete(f"/api/characters/{card_id}")
    assert (await client.delete(f"/api/worlds/{world['id']}")).status_code == 200

    with png.open("rb") as fh:
        parsed = (await client.post("/api/characters/import", files={"file": ("assistant.png", fh, "image/png")})).json()
    assert parsed["character_book"]["entries"] == []

    created = (
        await client.post(
            "/api/characters", json={"name": parsed["name"], "id": parsed["id"], "character_book": parsed["character_book"]}
        )
    ).json()
    assert created["world_id"], "re-imported card lost its lorebook link"

    restored = next(w for w in (await client.get("/api/worlds")).json() if w["id"] == created["world_id"])
    assert restored["name"] == "Things learned about the user"
    assert bool(restored["dynamic_enabled"]) is True


async def test_foreign_card_with_a_vestigial_empty_book_creates_no_world(client, db):
    """`entries: []` on a card Orb did not export is noise, not a lorebook."""
    created = (
        await client.post(
            "/api/characters", json={"name": "Foreign", "character_book": {"name": "Nothing", "entries": [], "extensions": {}}}
        )
    ).json()
    assert not created["world_id"]
    assert not [w for w in (await client.get("/api/worlds")).json() if w["name"] == "Nothing"]


async def test_failed_card_create_rolls_back_embedded_world(client, db):
    card_id = "existing-card"
    assert (await client.post("/api/characters", json={"id": card_id, "name": "Existing"})).status_code == 200

    await client.post_checked(
        "/api/characters",
        json={
            "id": card_id,
            "name": "Duplicate",
            "character_book": {
                "name": "Should not exist",
                "entries": [{"name": "A clue", "content": "A hidden detail", "keys": ["clue"]}],
            },
        },
        expected_status=400,
    )

    assert not [world for world in (await client.get("/api/worlds")).json() if world["name"] == "Should not exist"]
    assert (await client.get(f"/api/characters/{card_id}")).json()["name"] == "Existing"


async def test_embedded_book_creates_entries_and_reuses_world(client, db):
    book = {"name": "Shared lore", "entries": [{"name": "A clue", "content": "A hidden detail", "keys": ["clue"]}]}
    first = (await client.post("/api/characters", json={"name": "First", "character_book": book})).json()
    second = (await client.post("/api/characters", json={"name": "Second", "character_book": book})).json()

    assert first["world_id"] == second["world_id"]
    assert [world["name"] for world in (await client.get("/api/worlds")).json()] == ["Shared lore"]
    entries = (await client.get(f"/api/worlds/{first['world_id']}/entries")).json()
    assert [(entry["name"], entry["content"]) for entry in entries] == [("A clue", "A hidden detail")]


async def test_expression_upload_uses_expression_limit(client, db, monkeypatch):
    from backend.api.routes import characters

    card_id = await client.create("/api/characters", json={"name": "Faces"})
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("joy.png", b"image bytes" * 20)
    assert 100 < len(archive.getvalue()) < 1000
    monkeypatch.setattr(characters, "_MAX_EXPRESSION_UPLOAD", 1000)

    resp = await client.post_json(
        f"/api/characters/{card_id}/expressions", files={"file": ("expressions.zip", archive.getvalue(), "application/zip")}
    )
    assert resp == {"labels": ["joy"]}


async def test_card_render_projection_and_script_roundtrip(client, db, tmp_path):
    from backend.features.cards import parsing

    display = {
        "scriptName": "dialogue",
        "findRegex": "/<dialogue>/ig",
        "replaceString": '<div class="dialogue">',
        "placement": [2],
        "markdownOnly": True,
    }
    prompt = {"findRegex": "/secret/g", "replaceString": "", "placement": [2], "promptOnly": True}
    unflagged = {"findRegex": "/word/g", "replaceString": "shown", "placement": [1]}
    extensions = {
        "regex_scripts": [display, prompt, unflagged, {**display, "disabled": True}],
        "orb": {"display_css": ".dialogue { color: red; }"},
        "foreign": {"keep": True},
    }
    # Exercise the same parse -> schema -> persistence boundary as an imported card.
    source = tmp_path / "import.png"
    source.write_bytes(parsing.to_png({"name": "Scripted", "extensions": extensions}))
    imported = parsing.card_to_dict(parsing.parse(str(source)))
    created = (await client.post("/api/characters", json=imported)).json()
    card_id = created["id"]
    listed = next(card for card in (await client.get("/api/characters")).json() if card["id"] == card_id)
    assert "extensions" not in listed
    assert listed["display_scripts"] == [
        {key: s[key] for key in ("findRegex", "replaceString", "placement")} for s in (display, unflagged)
    ]
    assert listed["display_css"] == extensions["orb"]["display_css"]
    saved = (await client.get(f"/api/characters/{card_id}")).json()
    assert saved["extensions"] == extensions
    exported = tmp_path / "export.png"
    exported.write_bytes((await client.get(f"/api/characters/{card_id}/export")).content)
    assert parsing.card_to_dict(parsing.parse(str(exported)))["extensions"] == extensions
    # Editor changes use the existing extension update path and survive PNG export.
    display.update({"findRegex": "/<speech>/g", "replaceString": " \n$1\n ", "minDepth": 3})
    await client.put(f"/api/characters/{card_id}", json={"extensions": extensions})
    listed = next(card for card in (await client.get("/api/characters")).json() if card["id"] == card_id)
    assert listed["display_scripts"][0] == {key: display[key] for key in ("findRegex", "replaceString", "placement")}
    exported.write_bytes((await client.get(f"/api/characters/{card_id}/export")).content)
    assert parsing.card_to_dict(parsing.parse(str(exported)))["extensions"] == extensions
    extensions["orb"]["card_scripts_enabled"] = False
    await client.put(f"/api/characters/{card_id}", json={"extensions": extensions})
    listed = next(card for card in (await client.get("/api/characters")).json() if card["id"] == card_id)
    assert listed["display_scripts"] == []
    assert (await client.get(f"/api/characters/{card_id}")).json()["extensions"]["regex_scripts"] == extensions["regex_scripts"]


async def test_card_scripts_project_live_and_historical_prompts_without_persisting(client, llm_mock):
    from backend.database import get_messages
    from backend.pipeline import handle_turn

    greeting = '<div id="reader">artifact</div><div id="model" hidden>{{char}} waits.</div>'
    card = (
        await client.post(
            "/api/characters",
            json={
                "name": "Amy",
                "first_mes": greeting,
                "extensions": {
                    "regex_scripts": [
                        {
                            "findRegex": '/<div id="reader">.*?</div>/gs',
                            "replaceString": "",
                            "placement": [2],
                            "promptOnly": True,
                        },
                        {
                            "findRegex": '/<div id="model" hidden>(.*?)</div>/gs',
                            "replaceString": "$1",
                            "placement": [2],
                            "promptOnly": True,
                        },
                        {"findRegex": "/secret/g", "replaceString": "projected", "placement": [1], "promptOnly": True},
                    ]
                },
            },
        )
    ).json()
    conv = (await client.post("/api/conversations", json={"character_card_id": card["id"]})).json()
    cid = conv["id"]
    for body in ("secret", "next"):
        llm_mock.enqueue_writer("Reply.")
        events = [event async for event in handle_turn(cid, body)]
        assert not any(event.get("event") == "error" for event in events)
    writer_calls = [call for call in llm_mock.captured if call["pass"] == "writer"]
    assert len(writer_calls) == 2
    for call in writer_calls:
        prompt = json.dumps(call["messages"])
        assert "Amy waits." in prompt
        assert "projected" in prompt
        assert "secret" not in prompt
        assert "artifact" not in prompt
    stored = await get_messages(cid)
    assert stored[0]["content"] == greeting
    assert stored[1]["content"] == "secret"
