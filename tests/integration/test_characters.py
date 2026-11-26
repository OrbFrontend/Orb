import asyncio
import base64
import io
import json
import zipfile

from PIL import Image


async def test_character_create_read_and_validation(client, db):
    payload = {
        "name": "Lira",
        "description": "A wandering bard.",
        "personality": "Cheerful",
        "scenario": "A tavern",
        "tags": ["fantasy", "bard"],
        "alternate_greetings": ["Hello there.", "Good day, stranger."],
    }
    card_id = await client.create("/api/characters", json=payload)
    row = await db.one("SELECT name, description, tags, alternate_greetings FROM character_cards WHERE id = ?", (card_id,))
    assert (row["name"], row["description"]) == ("Lira", "A wandering bard.")
    assert json.loads(row["tags"]) == ["fantasy", "bard"]
    assert json.loads(row["alternate_greetings"]) == payload["alternate_greetings"]

    assert "Lira" in [c["name"] for c in await client.get_json("/api/characters")]
    got = await client.get_json(f"/api/characters/{card_id}")
    assert (got["name"], got["description"]) == ("Lira", "A wandering bard.")
    await client.get_checked("/api/characters/no-such-id", expected_status=404)
    await client.post_checked("/api/characters", json={"name": "   "}, expected_status=422)

    assert (await client.post_json("/api/characters", json={"id": "my-stable-id", "name": "Stable"}))["id"] == "my-stable-id"
    assert await db.one("SELECT id FROM character_cards WHERE id = 'my-stable-id'") is not None


async def test_character_update_persists_and_validates(client, db):
    created = await client.post_json("/api/characters", json={"name": "Old Name", "scenario": "Old scenario", "tags": ["o"]})
    card_id = created["id"]
    await asyncio.sleep(0.01)

    resp = await client.put_json(
        f"/api/characters/{card_id}", json={"name": "New Name", "scenario": "New scenario", "tags": ["action", "drama"]}
    )
    assert (resp["name"], resp["tags"]) == ("New Name", ["action", "drama"])
    row = await db.one("SELECT name, scenario, tags, updated_at FROM character_cards WHERE id = ?", (card_id,))
    assert (row["name"], row["scenario"]) == ("New Name", "New scenario")
    assert json.loads(row["tags"]) == ["action", "drama"]
    assert row["updated_at"] > created["updated_at"]

    await client.put_checked(f"/api/characters/{card_id}", json={"name": "  "}, expected_status=422)
    await client.put_checked("/api/characters/no-such-id", json={"name": "Ghost"}, expected_status=404)


async def test_character_updates_sync_to_linked_conversations(client, db):
    card_id = await client.create(
        "/api/characters", json={"name": "Old Name", "scenario": "Original scenario", "post_history_instructions": "Original"}
    )
    cid = await client.create("/api/conversations", json={"character_card_id": card_id})
    custom = await client.create("/api/conversations", json={"character_card_id": card_id, "title": "Custom Title"})
    assert (await db.one("SELECT title FROM conversations WHERE id = ?", (cid,)))["title"] == "Chat I"

    await client.put(
        f"/api/characters/{card_id}",
        json={"name": "New Name", "scenario": "Updated scenario", "post_history_instructions": "New instructions"},
    )

    row = await db.one(
        "SELECT title, character_name, character_scenario, post_history_instructions FROM conversations WHERE id = ?", (cid,)
    )
    assert tuple(row) == ("Chat I", "New Name", "Updated scenario", "New instructions")
    # A custom title is left alone; the character name still follows.
    row = await db.one("SELECT title, character_name FROM conversations WHERE id = ?", (custom,))
    assert tuple(row) == ("Custom Title", "New Name")


async def test_delete_character_keeps_conversations_unless_asked(client, db):
    for flag, kept in (("", True), ("?delete_conversations=true", False)):
        card_id = await client.create("/api/characters", json={"name": "Doomed"})
        cid = await client.create("/api/conversations", json={"character_card_id": card_id})
        await client.delete_checked(f"/api/characters/{card_id}{flag}")
        assert await db.one("SELECT id FROM character_cards WHERE id = ?", (card_id,)) is None
        # By default the conversation stays, with character_card_id left as a dangling reference.
        assert (await db.one("SELECT id FROM conversations WHERE id = ?", (cid,)) is not None) is kept


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
    assert (await client.get("/api/settings")).headers["cache-control"] == "no-store"


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


def _split_png_b64() -> str:
    """600x900: red top third, blue below."""
    image = Image.new("RGB", (600, 900), (40, 40, 200))
    image.paste((200, 40, 40), (0, 0, 600, 300))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _thumb_is_red(content: bytes) -> bool:
    r, _, b = Image.open(io.BytesIO(content)).convert("RGB").getpixel((96, 96))
    return r > b


async def test_avatar_thumbnail_crops_to_the_users_focus_until_the_avatar_changes(client, db):
    card_id = await client.create(
        "/api/characters", json={"name": "Split", "avatar_b64": _split_png_b64(), "avatar_mime": "image/png"}
    )
    centered = await client.get_checked(f"/api/characters/{card_id}/avatar/thumb")
    assert not _thumb_is_red(centered.content)

    focus = [0.25, 0.0, 0.75, 1 / 3]
    await client.put(f"/api/characters/{card_id}/avatar/focus", json={"focus": focus})
    focused = await client.get_checked(
        f"/api/characters/{card_id}/avatar/thumb", headers={"If-None-Match": centered.headers["etag"]}
    )
    assert _thumb_is_red(focused.content)
    assert (await client.get_json(f"/api/characters/{card_id}/avatar/focus"))["manual"] is True
    # Editing the card's text keeps the focus; a new avatar drops it.
    await client.put(f"/api/characters/{card_id}", json={"description": "edited"})
    assert _thumb_is_red((await client.get_checked(f"/api/characters/{card_id}/avatar/thumb")).content)
    await client.put(f"/api/characters/{card_id}", json={"avatar_b64": _split_png_b64(), "avatar_mime": "image/png"})
    assert not _thumb_is_red((await client.get_checked(f"/api/characters/{card_id}/avatar/thumb")).content)

    await client.put_checked(f"/api/characters/{card_id}/avatar/focus", json={"focus": [0.5, 0, 0.2, 1]}, expected_status=422)


async def test_avatar_thumbnail_is_immutable_only_at_the_listed_version(client, db):
    card_id = await client.create(
        "/api/characters", json={"name": "Split", "avatar_b64": _split_png_b64(), "avatar_mime": "image/png"}
    )

    async def listed() -> str:
        cards = await client.get_json("/api/characters")
        return next(c["avatar_thumb"] for c in cards if c["id"] == card_id)

    thumb = f"/api/characters/{card_id}/avatar/thumb"
    version = await listed()
    pinned = await client.get_checked(f"{thumb}?v={version}")
    assert "immutable" in pinned.headers["cache-control"]
    assert "immutable" not in (await client.get_checked(thumb)).headers["cache-control"]

    # A new crop is a new version; the old URL serves the new bytes, cached only briefly.
    await client.put(f"/api/characters/{card_id}/avatar/focus", json={"focus": [0.25, 0.0, 0.75, 1 / 3]})
    assert await listed() != version
    stale = await client.get_checked(f"{thumb}?v={version}")
    assert _thumb_is_red(stale.content)
    assert "immutable" not in stale.headers["cache-control"]


async def test_avatar_thumbnail_snaps_to_a_detected_head(client, db, monkeypatch):
    from backend.features.cards import avatar_focus
    from backend.inference.local_models.head_detector import Head

    calls = []

    def heads(image):
        calls.append(image.size)
        return [Head(left=200, top=50, right=400, bottom=250, score=0.9)]

    monkeypatch.setattr(avatar_focus.head_detector, "ready", lambda: (True, ""))
    monkeypatch.setattr(avatar_focus.head_detector, "heads", heads)
    card_id = await client.create(
        "/api/characters", json={"name": "Split", "avatar_b64": _split_png_b64(), "avatar_mime": "image/png"}
    )
    snapped = await client.get_checked(f"/api/characters/{card_id}/avatar/thumb")
    assert _thumb_is_red(snapped.content)
    # The stored box serves later requests without detecting again.
    assert _thumb_is_red((await client.get_checked(f"/api/characters/{card_id}/avatar/thumb")).content)
    assert calls == [(600, 900)]

    # Switched off, thumbnails return to the center crop.
    await client.post_json("/api/local-ml/head_detector/enabled", json={"enabled": False})
    assert not _thumb_is_red((await client.get_checked(f"/api/characters/{card_id}/avatar/thumb")).content)


def test_focus_square_frames_one_head_and_keeps_a_group_together():
    from backend.features.cards.avatar_focus import focus_square
    from backend.inference.local_models.head_detector import Head

    main = Head(left=200, top=100, right=400, bottom=300, score=0.9)
    bystander = Head(left=20, top=600, right=80, bottom=660, score=0.8)
    solo = focus_square(600, 900, [main, bystander])
    assert solo is not None
    left, top, right, bottom = solo
    assert left * 600 < 200 and right * 600 > 400 and top * 900 < 100 and bottom * 900 < 450

    partner = Head(left=380, top=420, right=560, bottom=600, score=0.85)
    pair = focus_square(600, 900, [main, partner])
    assert pair is not None
    assert pair[1] * 900 <= 100 and pair[3] * 900 >= 600

    # A tiny head is not enlarged past the thumbnail's resolution.
    tiny = focus_square(600, 900, [Head(left=290, top=290, right=310, bottom=310, score=0.9)])
    assert tiny is not None and (tiny[2] - tiny[0]) * 600 >= 160
    assert focus_square(600, 900, []) is None


async def test_list_characters_omits_heavy_text_fields(client, db):
    await client.post(
        "/api/characters", json={"name": "Heavy", "description": "x" * 100, "first_mes": "y" * 100, "scenario": "z" * 100}
    )
    card = next(c for c in await client.get_json("/api/characters") if c["name"] == "Heavy")
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

    got = await client.get_json(f"/api/characters/{card_id}")
    assert got["extensions"] == ext

    # An update that doesn't send extensions leaves them untouched.
    await client.put(f"/api/characters/{card_id}", json={"scenario": "new"})
    got = await client.get_json(f"/api/characters/{card_id}")
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
    got = await client.get_json(f"/api/characters/{card_id}")
    assert got["extensions"] == {}
    # Pre-migration rows have a NULL column, which must decode the same way.
    await db.execute("UPDATE character_cards SET extensions = NULL WHERE id = ?", (card_id,))
    await db.commit()
    got = await client.get_json(f"/api/characters/{card_id}")
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

    card = await client.get_json(f"/api/characters/{card_id}")
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
    world = await client.post_json("/api/worlds", json={"name": "Things learned about the user"})
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
        parsed = await client.post_json("/api/characters/import", files={"file": ("assistant.png", fh, "image/png")})
    assert parsed["character_book"]["entries"] == []

    created = await client.post_json(
        "/api/characters", json={"name": parsed["name"], "id": parsed["id"], "character_book": parsed["character_book"]}
    )
    assert created["world_id"], "re-imported card lost its lorebook link"

    restored = next(w for w in await client.get_json("/api/worlds") if w["id"] == created["world_id"])
    assert restored["name"] == "Things learned about the user"
    assert bool(restored["dynamic_enabled"]) is True


async def test_foreign_card_with_a_vestigial_empty_book_creates_no_world(client, db):
    """`entries: []` on a card Orb did not export is noise, not a lorebook."""
    created = await client.post_json(
        "/api/characters", json={"name": "Foreign", "character_book": {"name": "Nothing", "entries": [], "extensions": {}}}
    )
    assert not created["world_id"]
    assert not [w for w in await client.get_json("/api/worlds") if w["name"] == "Nothing"]


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

    assert not [world for world in await client.get_json("/api/worlds") if world["name"] == "Should not exist"]
    assert (await client.get_json(f"/api/characters/{card_id}"))["name"] == "Existing"


async def test_embedded_book_creates_entries_and_reuses_world(client, db):
    book = {"name": "Shared lore", "entries": [{"name": "A clue", "content": "A hidden detail", "keys": ["clue"]}]}
    first = await client.post_json("/api/characters", json={"name": "First", "character_book": book})
    second = await client.post_json("/api/characters", json={"name": "Second", "character_book": book})

    assert first["world_id"] == second["world_id"]
    assert [world["name"] for world in await client.get_json("/api/worlds")] == ["Shared lore"]
    entries = await client.get_json(f"/api/worlds/{first['world_id']}/entries")
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
    card_id = (await client.post_json("/api/characters", json=imported))["id"]
    listed = next(card for card in await client.get_json("/api/characters") if card["id"] == card_id)
    assert "extensions" not in listed
    assert listed["display_scripts"] == [
        {key: s[key] for key in ("findRegex", "replaceString", "placement")} for s in (display, unflagged)
    ]
    assert listed["display_css"] == extensions["orb"]["display_css"]
    assert (await client.get_json(f"/api/characters/{card_id}"))["extensions"] == extensions
    exported = tmp_path / "export.png"
    exported.write_bytes((await client.get(f"/api/characters/{card_id}/export")).content)
    assert parsing.card_to_dict(parsing.parse(str(exported)))["extensions"] == extensions
    # Editor changes use the existing extension update path and survive PNG export.
    display.update({"findRegex": "/<speech>/g", "replaceString": " \n$1\n ", "minDepth": 3})
    await client.put(f"/api/characters/{card_id}", json={"extensions": extensions})
    listed = next(card for card in await client.get_json("/api/characters") if card["id"] == card_id)
    assert listed["display_scripts"][0] == {key: display[key] for key in ("findRegex", "replaceString", "placement")}
    exported.write_bytes((await client.get(f"/api/characters/{card_id}/export")).content)
    assert parsing.card_to_dict(parsing.parse(str(exported)))["extensions"] == extensions
    extensions["orb"]["card_scripts_enabled"] = False
    await client.put(f"/api/characters/{card_id}", json={"extensions": extensions})
    listed = next(card for card in await client.get_json("/api/characters") if card["id"] == card_id)
    assert listed["display_scripts"] == []
    assert (await client.get_json(f"/api/characters/{card_id}"))["extensions"]["regex_scripts"] == extensions["regex_scripts"]


async def test_card_scripts_project_live_and_historical_prompts_without_persisting(client, llm_mock):
    from backend.database import get_messages
    from backend.pipeline import handle_turn

    greeting = '<div id="reader">artifact</div><div id="model" hidden>{{char}} waits.</div>'
    card = await client.post_json(
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
    cid = (await client.post_json("/api/conversations", json={"character_card_id": card["id"]}))["id"]
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
