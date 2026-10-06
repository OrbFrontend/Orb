import backend.database as dbmod


async def test_create_conversation_persists_to_db(client, db):
    cid = await client.create("/api/conversations", json={"title": "My Chat"})
    assert cid

    row = await db.one("SELECT title FROM conversations WHERE id = ?", (cid,))
    assert row is not None
    assert row["title"] == "My Chat"


async def test_create_conversation_also_seeds_director_state(client, db):
    cid = await client.create("/api/conversations", json={})

    row = await db.one("SELECT active_moods FROM director_state WHERE conversation_id = ?", (cid,))
    assert row is not None
    assert row["active_moods"] == "[]"


async def test_list_conversations_includes_created(client, db):
    await client.post("/api/conversations", json={"title": "Listed"})
    resp = await client.get_json("/api/conversations")
    assert "Listed" in [c["title"] for c in resp]


async def test_list_conversations_message_count_excludes_swiped_branches(client, db):
    # message_count reflects only the active branch the user can swipe through,
    # not off-path siblings left behind by regenerating/swiping.
    cid = "conv-swipe-count"
    await dbmod.create_conversation(cid, "Swipe", "Nova", "")
    u1, _ = await dbmod.add_message(cid, "user", "hi", 0, parent_id=None)
    a_active, _ = await dbmod.add_message(cid, "assistant", "active", 1, parent_id=u1)
    await dbmod.add_message(cid, "assistant", "swipe", 1, parent_id=u1)  # off-path sibling
    await dbmod.set_active_leaf(cid, a_active)

    assert next(c for c in (await client.get("/api/conversations")).json() if c["id"] == cid)["message_count"] == 2


async def test_list_conversations_previews_the_latest_message(client, db):
    # A preview, not the message: the list carried every chat's whole last reply, when the sidebar shows a line of it.
    cid = "conv-preview"
    await dbmod.create_conversation(cid, "Preview", "Nova", "")
    u1, _ = await dbmod.add_message(cid, "user", "first", 0, parent_id=None)
    await dbmod.add_message(cid, "assistant", "é" + "x" * 999, 1, parent_id=u1)

    resp = await client.get("/api/conversations")
    assert next(c for c in resp.json() if c["id"] == cid)["last_message_preview"] == "é" + "x" * 199


async def test_delete_conversation_removes_from_db(client, db):
    cid = await client.create("/api/conversations", json={"title": "ToDelete"})

    await client.delete_checked(f"/api/conversations/{cid}")

    assert (await db.one("SELECT id FROM conversations WHERE id = ?", (cid,))) is None


async def test_delete_nonexistent_conversation_returns_404(client, db):
    await client.delete_checked("/api/conversations/no-such-conv", expected_status=404)


async def test_get_messages_on_new_conversation_returns_empty(client, db):
    cid = await client.create("/api/conversations", json={})

    assert (await client.get_json(f"/api/conversations/{cid}/messages")) == []


async def test_conversation_with_first_mes_creates_assistant_message(client, db):
    cid = await client.create("/api/conversations", json={"title": "Greeted", "first_mes": "Hello, traveller."})

    messages = await client.get_json(f"/api/conversations/{cid}/messages")
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "Hello, traveller."

    # Verify message is in DB
    row = await db.one("SELECT role, content FROM messages WHERE conversation_id = ?", (cid,))
    assert row["role"] == "assistant"
    assert row["content"] == "Hello, traveller."


async def test_touch_conversation_bumps_access_not_update(client, db):
    cid = await client.create("/api/conversations", json={})

    async with db.execute("SELECT updated_at, last_accessed_at FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
        updated_before, accessed_before = row["updated_at"], row["last_accessed_at"]

    import asyncio

    await asyncio.sleep(0.01)  # ensure clock advances

    await client.post_checked(f"/api/conversations/{cid}/touch")

    async with db.execute("SELECT updated_at, last_accessed_at FROM conversations WHERE id = ?", (cid,)) as cur:
        row = await cur.fetchone()
        updated_after, accessed_after = row["updated_at"], row["last_accessed_at"]

    # Opening a conversation is an access, not an edit: touch moves last_accessed_at forward (strict >, microsecond ISO strings
    # + the 0.01s sleep) while leaving updated_at -- the "content changed" timestamp -- alone.
    assert accessed_after > (accessed_before or "")
    assert updated_after == updated_before


async def test_conversation_with_character_card(client, db):
    card_id = await client.create(
        "/api/characters",
        json={
            "name": "Aria",
            "description": "An elf ranger.",
            "first_mes": "Greetings from the forest.",
            "scenario": "Deep woods",
        },
    )

    conv = await client.post_json("/api/conversations", json={"character_card_id": card_id})
    cid = conv["id"]
    assert conv["title"] == "Aria"
    assert conv["character_name"] == "Aria"

    # first_mes should be auto-added as the first assistant message
    assert (await client.get(f"/api/conversations/{cid}/messages")).json()[0]["content"] == "Greetings from the forest."

    # Verify link in DB
    assert (await db.one("SELECT character_card_id FROM conversations WHERE id = ?", (cid,)))["character_card_id"] == card_id


async def test_checkpoint_duplicates_active_path(client, db):
    cid = "conv-checkpoint-src"
    await dbmod.create_conversation(cid, "My Story", "Bot", "a scenario")
    u1, _ = await dbmod.add_message(
        cid,
        "user",
        "hello",
        0,
        parent_id=None,
        attachments=[{"mime_type": "image/png", "data_b64": "QUJD", "filename": "a.png", "size": 3}],
    )
    a1, _ = await dbmod.add_message(cid, "assistant", "hi there", 1, parent_id=u1)
    await dbmod.set_active_leaf(cid, a1)
    await dbmod.update_director_state(cid, ["tense"], keywords=["k"])
    await dbmod.add_conversation_log(cid, 0, [], ["tense"], "inj block", 12, message_id=a1, feedback={})
    # State history on the path: an agent value on the reply, then a user revision.
    await dbmod.add_state_events(
        cid,
        a1,
        [{"fragment_id": "hp", "entry_id": "e-hp", "op": "add", "text": "5", "fragment_label": "HP", "source": "agent"}],
    )
    await dbmod.add_state_events(
        cid,
        a1,
        [{"fragment_id": "hp", "entry_id": "e-hp", "op": "revise", "text": "6", "fragment_label": "HP", "source": "user"}],
    )

    new = await client.post_json(f"/api/conversations/{cid}/checkpoint", json={})
    new_cid = new["id"]
    assert new_cid != cid
    assert new["title"] == "My Story (checkpoint)"

    msgs = await client.get_json(f"/api/conversations/{new_cid}/messages")
    assert [(m["role"], m["content"], m["turn_index"]) for m in msgs] == [("user", "hello", 0), ("assistant", "hi there", 1)]
    # Fresh row ids -- the copy is a distinct message tree, not a shared reference.
    assert msgs[1]["id"] != a1
    # User upload carried onto the copy.
    upload = msgs[0]["user_attachments"][0]
    assert (await client.get(f"/api/user-attachments/{upload['id']}/content")).content == b"ABC"

    # Director state carried verbatim so continuation behaves identically.
    assert (await dbmod.get_director_state(new_cid))["active_moods"] == ["tense"]

    # The path's state history is copied and re-anchored, keeping entry ids and
    # sources, so the checkpoint starts from the source's state and history.
    copied = await dbmod.get_state_events_for_message(msgs[1]["id"])
    assert [(e["op"], e["entry_id"], e["text"], e["source"]) for e in copied] == [
        ("add", "e-hp", "5", "agent"),
        ("revise", "e-hp", "6", "user"),
    ]
    view = await dbmod.fold_path_state(new_cid, [m["id"] for m in msgs])
    assert [e.text for e in view.active("hp")] == ["6"]

    # Inspector log carried and re-pointed onto the copied assistant message.
    log = await dbmod.get_director_log_for_message(msgs[1]["id"])
    assert log is not None
    assert log["injection_block"] == "inj block"

    # Source conversation is untouched.
    assert len(await client.get_json(f"/api/conversations/{cid}/messages")) == 2


async def test_checkpoint_copies_only_active_branch(client, db):
    cid = "conv-checkpoint-branch"
    await dbmod.create_conversation(cid, "Branched", "Bot", "scenario")
    u1, _ = await dbmod.add_message(cid, "user", "prompt", 0, parent_id=None)
    a_active, _ = await dbmod.add_message(cid, "assistant", "active reply", 1, parent_id=u1)
    await dbmod.add_message(cid, "assistant", "swipe reply", 1, parent_id=u1)  # alternate branch
    await dbmod.set_active_leaf(cid, a_active)

    new_cid = await client.create(f"/api/conversations/{cid}/checkpoint", json={})

    msgs = await client.get_json(f"/api/conversations/{new_cid}/messages")
    assert [m["content"] for m in msgs] == ["prompt", "active reply"]
    # Only the active path is copied -- the alternate swipe is not carried.
    async with db.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (new_cid,)) as cur:
        assert (await cur.fetchone())["n"] == 2


async def test_checkpoint_accepts_custom_title(client, db):
    cid = "conv-checkpoint-title"
    await dbmod.create_conversation(cid, "Orig", "Bot", "scenario")
    m, _ = await dbmod.add_message(cid, "assistant", "hi", 0, parent_id=None)
    await dbmod.set_active_leaf(cid, m)

    resp = await client.post_json(f"/api/conversations/{cid}/checkpoint", json={"title": "  Saved Point  "})
    assert resp["title"] == "Saved Point"


async def test_checkpoint_missing_conversation_returns_404(client, db):
    await client.post_checked("/api/conversations/no-such-conv/checkpoint", json={}, expected_status=404)
