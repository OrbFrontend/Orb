from __future__ import annotations

import json

from backend.database import add_message, get_messages, set_active_leaf


async def _card(client, name: str, **extra) -> str:
    response = await client.post("/api/characters", json={"name": name, **extra})
    assert response.status_code == 200
    return response.json()["id"]


def _direct_scene(**arguments) -> list[dict]:
    return [{"type": "function", "function": {"name": "direct_scene", "arguments": arguments}}]


def _sse_events(body: str) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    name = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            raw = line[6:]
            try:
                data: object = json.loads(raw)
            except json.JSONDecodeError:
                data = raw
            events.append((name, data))
    return events


async def test_group_creation_allocates_durable_members(client, db):
    aria = await _card(client, "Aria")
    kael = await _card(client, "Kael")
    response = await client.post(
        "/api/conversations",
        json={
            "kind": "group",
            "title": "Campfire",
            "group_turn_mode": "round_robin",
            "group_max_speakers": 2,
            "members": [{"character_card_id": aria}, {"character_card_id": kael}],
        },
    )
    assert response.status_code == 200
    conv = response.json()
    assert conv["kind"] == "group"
    assert conv["character_card_id"] is None
    members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    assert [(m["speaker_key"], m["display_name"]) for m in members] == [("aria", "Aria"), ("kael", "Kael")]
    assert len({m["id"] for m in members}) == 2


async def test_conversion_stamps_existing_assistant_identity(client, db):
    card_id = await _card(client, "Solo")
    conv = (await client.post("/api/conversations", json={"character_card_id": card_id})).json()
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content, turn_index, created_at) VALUES (?, 'assistant', 'hello', 0, 'now')",
        (conv["id"],),
    )
    await db.commit()
    response = await client.post(f"/api/conversations/{conv['id']}/convert-to-group")
    assert response.status_code == 200
    member_id = response.json()["member"]["id"]
    row = await (await db.execute("SELECT speaker_member_id FROM messages WHERE conversation_id = ?", (conv["id"],))).fetchone()
    assert row["speaker_member_id"] == member_id


async def test_roster_removal_tombstones_and_readd_gets_new_identity(client):
    card_id = await _card(client, "Echo")
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "members": [{"character_card_id": card_id}]},
        )
    ).json()
    original = (await client.get(f"/api/conversations/{conv['id']}/members")).json()[0]
    narrator = {
        "display_name": "Narrator",
        "member_kind": "narrator",
    }
    assert (await client.put(f"/api/conversations/{conv['id']}/members", json={"members": [narrator]})).status_code == 200
    response = await client.put(
        f"/api/conversations/{conv['id']}/members",
        json={"members": [narrator, {"character_card_id": card_id}]},
    )
    assert response.status_code == 200
    readded = next(member for member in response.json() if member["character_card_id"] == card_id)
    assert readded["id"] != original["id"]
    history = (await client.get(f"/api/conversations/{conv['id']}/members?include_inactive=true")).json()
    assert next(member for member in history if member["id"] == original["id"])["active"] == 0


async def test_public_profile_merge_preserves_other_orb_extensions(client):
    card_id = await _card(
        client,
        "Profiled",
        extensions={"orb": {"fragments": {"mood": []}, "v3": {"nickname": "P"}}, "vendor": {"x": 1}},
    )
    response = await client.put(
        f"/api/characters/{card_id}/public-profile",
        json={"appearance": "Silver hair", "role": "Scout"},
    )
    assert response.status_code == 200
    card = (await client.get(f"/api/characters/{card_id}")).json()
    assert card["extensions"]["orb"]["public_profile"] == {"appearance": "Silver hair", "role": "Scout"}
    assert card["extensions"]["orb"]["v3"] == {"nickname": "P"}
    assert card["extensions"]["vendor"] == {"x": 1}


async def test_director_group_beat_streams_and_persists_an_ordered_message_chain(client, llm_mock):
    aria = await _card(client, "Aria", description="ARIA PRIVATE")
    kael = await _card(client, "Kael", description="KAEL PRIVATE")
    conv = (
        await client.post(
            "/api/conversations",
            json={
                "kind": "group",
                "title": "Campfire",
                "members": [{"character_card_id": aria}, {"character_card_id": kael}],
            },
        )
    ).json()
    members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    by_name = {member["display_name"]: member for member in members}
    llm_mock.enqueue_director(
        _direct_scene(
            moods=[],
            speaking_plan=["aria — Notice the trail", "kael — Explain the ward"],
        )
    )
    llm_mock.enqueue_writer("**Aria:**\nI found tracks.")
    llm_mock.enqueue_writer("Kael: The ward is broken.")

    response = await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})
    assert response.status_code == 200
    events = _sse_events(response.text)
    names = [name for name, _ in events]
    assert names.count("speaking_plan") == 1
    assert names.count("speaker_start") == 2
    assert names.count("speaker_done") == 2
    assert names[-1] == "done"

    rows = await get_messages(conv["id"])
    user, first, second = rows[-3:]
    assert [first["speaker_member_id"], second["speaker_member_id"]] == [
        by_name["Aria"]["id"],
        by_name["Kael"]["id"],
    ]
    assert first["content"] == "I found tracks."
    assert second["content"] == "The ward is broken."
    assert first["parent_id"] == user["id"] and second["parent_id"] == first["id"]
    assert user["beat_id"] == first["beat_id"] == second["beat_id"]

    writers = [call for call in llm_mock.captured if call["pass"] == "writer"]
    assert "ARIA PRIVATE" in json.dumps(writers[0]["messages"])
    assert "KAEL PRIVATE" not in json.dumps(writers[0]["messages"])
    assert "KAEL PRIVATE" in json.dumps(writers[1]["messages"])


async def test_manual_group_requires_pin_before_calling_llm(client, llm_mock):
    aria = await _card(client, "Aria")
    conv = (
        await client.post(
            "/api/conversations",
            json={
                "kind": "group",
                "group_turn_mode": "manual",
                "members": [{"character_card_id": aria}],
            },
        )
    ).json()
    response = await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "Hello"})
    assert response.status_code == 200
    assert any(name == "error" for name, _ in _sse_events(response.text))
    assert llm_mock.calls == []
    assert await get_messages(conv["id"]) == []


async def test_atomic_roster_sync_allows_cards_to_swap_existing_member_slots(client):
    aria = await _card(client, "Aria")
    kael = await _card(client, "Kael")
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "members": [{"character_card_id": aria}, {"character_card_id": kael}]},
        )
    ).json()
    members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    response = await client.put(
        f"/api/conversations/{conv['id']}/members",
        json={
            "members": [
                {**members[0], "character_card_id": kael},
                {**members[1], "character_card_id": aria},
            ]
        },
    )
    assert response.status_code == 200
    assert [member["character_card_id"] for member in response.json()] == [kael, aria]


async def test_group_compress_remaps_speaker_ids_and_preserves_beat_ids(client):
    aria = await _card(client, "Aria")
    kael = await _card(client, "Kael")
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "members": [{"character_card_id": aria}, {"character_card_id": kael}]},
        )
    ).json()
    old_members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    beat_id = "beat-copy"
    user_id, _ = await add_message(conv["id"], "user", "Question", 0, beat_id=beat_id)
    first_id, _ = await add_message(
        conv["id"], "assistant", "Aria reply", 1, parent_id=user_id, speaker_member_id=old_members[0]["id"], beat_id=beat_id
    )
    second_id, _ = await add_message(
        conv["id"], "assistant", "Kael reply", 2, parent_id=first_id, speaker_member_id=old_members[1]["id"], beat_id=beat_id
    )
    await set_active_leaf(conv["id"], second_id)

    response = await client.post(
        f"/api/conversations/{conv['id']}/compress",
        json={"summary": "Summary with Aria and Kael attribution.", "keep_count": 2},
    )
    assert response.status_code == 200
    new_cid = response.json()["new_conversation_id"]
    new_members = (await client.get(f"/api/conversations/{new_cid}/members?include_inactive=true")).json()
    rows = await get_messages(new_cid)
    assert rows[0]["speaker_member_id"] is None
    assert rows[0]["content"].startswith("Summary with Aria")
    assert [row["beat_id"] for row in rows[1:]] == [beat_id, beat_id]
    assert [row["speaker_member_id"] for row in rows[1:]] == [new_members[0]["id"], new_members[1]["id"]]
    assert not {row["speaker_member_id"] for row in rows[1:]} & {member["id"] for member in old_members}


async def test_group_summarize_labels_history_and_context_size_is_a_maximum(client, llm_mock):
    aria = await _card(client, "Aria", description="short private")
    kael = await _card(client, "Kael", description="KAEL LARGEST PRIVATE SHEET " * 10)
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "members": [{"character_card_id": aria}, {"character_card_id": kael}]},
        )
    ).json()
    members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    parent = None
    for index, (role, content, speaker) in enumerate(
        [
            ("user", "One", None),
            ("assistant", "Two", members[0]["id"]),
            ("assistant", "Three", members[1]["id"]),
            ("user", "Four", None),
            ("assistant", "Five", members[0]["id"]),
        ]
    ):
        parent, _ = await add_message(
            conv["id"], role, content, index, parent_id=parent, speaker_member_id=speaker, beat_id=f"beat-{index}"
        )
    await set_active_leaf(conv["id"], parent)
    llm_mock.enqueue_writer("A summary.")
    response = await client.post(f"/api/conversations/{conv['id']}/summarize", json={"keep_count": 2})
    assert response.status_code == 200
    prompt = json.dumps(llm_mock.captured[-1]["messages"])
    assert "Aria: Two" in prompt and "Kael: Three" in prompt

    context = (await client.get(f"/api/conversations/{conv['id']}/context-size")).json()
    assert context["estimate_kind"] == "maximum"
    assert context["breakdown"]["largest_speaker_tail"]["chars"] >= len(("KAEL LARGEST PRIVATE SHEET " * 10).strip())


async def test_group_activation_enables_cast_worlds_and_preserves_floating_worlds(client):
    aria_world = (await client.post("/api/worlds", json={"name": "Aria lore"})).json()
    kael_world = (await client.post("/api/worlds", json={"name": "Kael lore"})).json()
    other_world = (await client.post("/api/worlds", json={"name": "Other character lore"})).json()
    floating_on = (await client.post("/api/worlds", json={"name": "Global lore"})).json()
    floating_off = (await client.post("/api/worlds", json={"name": "Retired global lore"})).json()
    aria = await _card(client, "Aria", world_id=aria_world["id"])
    kael = await _card(client, "Kael", world_id=kael_world["id"])
    await _card(client, "Other", world_id=other_world["id"])
    await client.put(f"/api/worlds/{aria_world['id']}", json={"enabled": False})
    await client.put(f"/api/worlds/{kael_world['id']}", json={"enabled": False})
    await client.put(f"/api/worlds/{floating_off['id']}", json={"enabled": False})
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "members": [{"character_card_id": aria}, {"character_card_id": kael}]},
        )
    ).json()
    before = {world["id"]: world for world in (await client.get("/api/worlds")).json()}

    response = await client.post(f"/api/conversations/{conv['id']}/activate")
    assert response.status_code == 200
    assert set(response.json()["world_ids"]) == {aria_world["id"], kael_world["id"]}
    after = {world["id"]: world for world in (await client.get("/api/worlds")).json()}
    assert after[aria_world["id"]]["enabled"] and after[kael_world["id"]]["enabled"]
    assert not after[other_world["id"]]["enabled"]
    assert after[floating_on["id"]]["enabled"]
    assert not after[floating_off["id"]]["enabled"]
    for world_id in after:
        assert after[world_id]["updated_at"] == before[world_id]["updated_at"]
        assert after[world_id]["content_revision"] == before[world_id]["content_revision"]


async def test_group_regenerate_and_magic_rewrite_keep_target_speaker_and_parent(client, db, llm_mock):
    aria = await _card(client, "Aria")
    kael = await _card(client, "Kael")
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "members": [{"character_card_id": aria}, {"character_card_id": kael}]},
        )
    ).json()
    members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    user_id, _ = await add_message(conv["id"], "user", "Question", 0, beat_id="original")
    first_id, _ = await add_message(
        conv["id"], "assistant", "First", 1, parent_id=user_id, speaker_member_id=members[0]["id"], beat_id="original"
    )
    target_id, _ = await add_message(
        conv["id"], "assistant", "Second", 2, parent_id=first_id, speaker_member_id=members[1]["id"], beat_id="original"
    )
    await set_active_leaf(conv["id"], target_id)

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=[]))
    llm_mock.enqueue_writer("Kael: Replacement")
    response = await client.post(f"/api/conversations/{conv['id']}/messages/{target_id}/regenerate", json={})
    assert response.status_code == 200
    replacement = await (
        await db.execute("SELECT * FROM messages WHERE parent_id = ? ORDER BY id DESC LIMIT 1", (first_id,))
    ).fetchone()
    assert replacement["id"] != target_id
    assert replacement["speaker_member_id"] == members[1]["id"]
    assert replacement["content"] == "Replacement"

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=[]))
    llm_mock.enqueue_writer("**Kael:** Rewritten")
    response = await client.post(
        f"/api/conversations/{conv['id']}/messages/{target_id}/magic_rewrite",
        json={"direction": "Make it quieter"},
    )
    assert response.status_code == 200
    rewritten = await (
        await db.execute("SELECT * FROM messages WHERE parent_id = ? ORDER BY id DESC LIMIT 1", (first_id,))
    ).fetchone()
    assert rewritten["id"] not in (target_id, replacement["id"])
    assert rewritten["speaker_member_id"] == members[1]["id"]
    assert rewritten["content"] == "Rewritten"


async def test_group_delete_preview_counts_invisible_sibling_replies(client):
    card_id = await _card(client, "Aria")
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "members": [{"character_card_id": card_id}]},
        )
    ).json()
    member = (await client.get(f"/api/conversations/{conv['id']}/members")).json()[0]
    user_id, _ = await add_message(conv["id"], "user", "Question", 0)
    first_id, _ = await add_message(conv["id"], "assistant", "Visible", 1, parent_id=user_id, speaker_member_id=member["id"])
    sibling_id, _ = await add_message(
        conv["id"], "assistant", "Hidden sibling", 1, parent_id=user_id, speaker_member_id=member["id"]
    )
    await add_message(conv["id"], "assistant", "Hidden descendant", 2, parent_id=sibling_id, speaker_member_id=member["id"])
    await set_active_leaf(conv["id"], first_id)

    response = await client.get(f"/api/conversations/{conv['id']}/messages/{first_id}/delete-preview")
    assert response.status_code == 200
    assert response.json() == {"message_count": 3, "assistant_count": 3}


async def test_group_fork_edit_runs_a_fresh_beat_from_the_new_user_sibling(client, db, llm_mock):
    aria = await _card(client, "Aria")
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "members": [{"character_card_id": aria}]},
        )
    ).json()
    member = (await client.get(f"/api/conversations/{conv['id']}/members")).json()[0]
    user_id, _ = await add_message(conv["id"], "user", "Old question", 0, beat_id="old")
    old_reply, _ = await add_message(
        conv["id"], "assistant", "Old reply", 1, parent_id=user_id, speaker_member_id=member["id"], beat_id="old"
    )
    await set_active_leaf(conv["id"], old_reply)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Answer the edit"]))
    llm_mock.enqueue_writer("Fresh reply")
    response = await client.post(
        f"/api/conversations/{conv['id']}/messages/{user_id}/fork-edit",
        json={"content": "New question"},
    )
    assert response.status_code == 200
    new_user = await (
        await db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1", (conv["id"],)
        )
    ).fetchone()
    new_reply = await (await db.execute("SELECT * FROM messages WHERE parent_id = ?", (new_user["id"],))).fetchone()
    assert new_user["id"] != user_id and new_user["beat_id"] != "old"
    assert new_reply["speaker_member_id"] == member["id"]
    assert new_reply["beat_id"] == new_user["beat_id"]
