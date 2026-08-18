from __future__ import annotations

import json

import pytest

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


async def test_group_list_includes_active_cast_names_in_roster_order(client, db):
    aria = await _card(client, "Aria")
    kael = await _card(client, "Kael")
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

    listed = (await client.get("/api/conversations")).json()
    row = next(item for item in listed if item["id"] == conv["id"])
    assert row["group_member_names"] == ["Aria", "Kael"]


async def test_group_settings_update_round_trips_every_scene_field(client):
    """Group settings edits the whole durable scene config, including the shared
    style instructions — which were previously write-once at creation."""
    aria = await _card(client, "Aria")
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "title": "Campfire", "members": [{"character_card_id": aria}]},
        )
    ).json()
    response = await client.put(
        f"/api/conversations/{conv['id']}",
        json={
            "title": "The Long Watch",
            "group_turn_mode": "round_robin",
            "group_max_speakers": 5,
            "character_scenario": "A cold night on the wall.",
            "post_history_instructions": "Keep the prose terse.",
        },
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "The Long Watch"
    assert updated["group_turn_mode"] == "round_robin"
    assert updated["group_max_speakers"] == 5
    assert updated["character_scenario"] == "A cold night on the wall."
    assert updated["post_history_instructions"] == "Keep the prose terse."
    reloaded = next(c for c in (await client.get("/api/conversations")).json() if c["id"] == conv["id"])
    assert reloaded["post_history_instructions"] == "Keep the prose terse."


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


async def test_every_speaker_in_a_beat_sees_the_user_s_image(client, llm_mock):
    """An upload answers the whole cast, not just whoever speaks first.

    The first speaker receives it as its own trailing attachment; every later one
    only ever sees it through the replayed user row, so the row has to carry it.
    """
    aria = await _card(client, "Aria")
    kael = await _card(client, "Kael")
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
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Look", "kael — Look too"]))
    llm_mock.enqueue_writer("Aria speaks.")
    llm_mock.enqueue_writer("Kael speaks.")

    pixel = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNiAAAABgADNjd8qAAAAABJRU5ErkJggg=="
    response = await client.post(
        f"/api/conversations/{conv['id']}/send",
        json={"content": "What is this?", "attachments": [{"b64": pixel, "mime": "image/png", "filename": "map.png"}]},
    )
    assert response.status_code == 200

    writers = [call for call in llm_mock.captured if call["pass"] == "writer"]
    assert len(writers) == 2
    for writer in writers:
        assert pixel in json.dumps(writer["messages"]), "a speaker was asked about an image it never saw"


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


async def test_summarizing_a_renamed_group_calls_it_by_its_current_name(client, llm_mock):
    """`{{char}}` is the scene's title in a group. The title is editable and
    `character_name` keeps the founding name, so reading the wrong one has the
    summary narrating a scene under a name the user has already replaced."""
    aria = await _card(client, "Aria")
    conv = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "title": "Campfire", "members": [{"character_card_id": aria}]},
        )
    ).json()
    await client.put(
        f"/api/conversations/{conv['id']}",
        json={"title": "The Long Watch", "character_scenario": "{{char}} opens at dusk."},
    )
    parent = None
    for index, (role, content) in enumerate([("user", "One"), ("assistant", "Two"), ("user", "Three"), ("assistant", "Four")]):
        parent, _ = await add_message(conv["id"], role, content, index, parent_id=parent)
    await set_active_leaf(conv["id"], parent)
    llm_mock.enqueue_writer("A summary.")

    response = await client.post(f"/api/conversations/{conv['id']}/summarize", json={"keep_count": 2})
    assert response.status_code == 200
    prompt = json.dumps(llm_mock.captured[-1]["messages"])
    assert "The Long Watch opens at dusk." in prompt
    assert "Campfire" not in prompt


# ── Character context modes ─────────────────────────────────────────────────


async def _two_card_group(
    client,
    *,
    context_mode: str | None = None,
    aria_extra: dict | None = None,
    kael_extra: dict | None = None,
) -> tuple[dict, list[dict]]:
    aria = await _card(client, "Aria", **{"description": "ARIA PRIVATE", "mes_example": "ARIA EXAMPLE", **(aria_extra or {})})
    kael = await _card(client, "Kael", **{"description": "KAEL PRIVATE", "mes_example": "KAEL EXAMPLE", **(kael_extra or {})})
    payload = {
        "kind": "group",
        "title": "Campfire",
        "members": [{"character_card_id": aria}, {"character_card_id": kael}],
    }
    conv = (await client.post("/api/conversations", json=payload)).json()
    if context_mode:
        response = await client.put(f"/api/conversations/{conv['id']}", json={"group_context_mode": context_mode})
        assert response.status_code == 200
        conv = response.json()
    members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    return conv, members


async def _run_two_speaker_beat(client, llm_mock, conv):
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail", "kael — Explain the ward"]))
    llm_mock.enqueue_writer("I found tracks.")
    llm_mock.enqueue_writer("The ward is broken.")
    response = await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})
    assert response.status_code == 200
    return response


def _systems(llm_mock, pass_name: str) -> list[str]:
    return [str(call["messages"][0]["content"]) for call in llm_mock.captured if call["pass"] == pass_name]


async def test_group_context_mode_defaults_to_private_and_rejects_unknown_values(client):
    conv, _ = await _two_card_group(client)
    assert conv["group_context_mode"] == "private"

    response = await client.put(
        f"/api/conversations/{conv['id']}",
        json={"title": "Renamed", "group_context_mode": "everyone_sees_everything"},
    )
    assert response.status_code == 422
    # A rejected payload must not half-apply: the title edit rode the same call.
    reloaded = (await client.get("/api/conversations")).json()
    assert next(item for item in reloaded if item["id"] == conv["id"])["title"] == "Campfire"

    for mode in ("shared", "swap", "private"):
        response = await client.put(f"/api/conversations/{conv['id']}", json={"group_context_mode": mode})
        assert response.status_code == 200
        assert response.json()["group_context_mode"] == mode


async def test_creation_can_pick_a_context_mode_and_omitting_it_stays_private(client):
    """The New group modal offers the control alongside the other durable scene
    settings, so the create payload has to carry it — not just the update path."""
    aria = await _card(client, "Aria", description="ARIA PRIVATE")
    base = {"kind": "group", "title": "Campfire", "members": [{"character_card_id": aria}]}

    created = (await client.post("/api/conversations", json={**base, "group_context_mode": "shared"})).json()
    assert created["group_context_mode"] == "shared"

    # Omitted entirely — every non-group caller and the convert flow rely on this.
    assert (await client.post("/api/conversations", json=base)).json()["group_context_mode"] == "private"

    rejected = await client.post("/api/conversations", json={**base, "group_context_mode": "everyone_sees_everything"})
    assert rejected.status_code == 422


async def test_solo_conversations_are_unaffected_by_the_column(client, llm_mock):
    card_id = await _card(client, "Solo", description="SOLO PRIVATE")
    conv = (await client.post("/api/conversations", json={"character_card_id": card_id})).json()
    assert conv["group_context_mode"] == "private"
    llm_mock.enqueue_writer("A reply.")
    assert (await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "Hi"})).status_code == 200
    system = _systems(llm_mock, "writer")[0]
    assert "## Cast" not in system and "## Character: Solo" in system


async def test_context_mode_rides_checkpoint_and_compression_forks(client):
    conv, members = await _two_card_group(client, context_mode="shared")
    parent = None
    for index, (role, speaker) in enumerate([("user", None), ("assistant", members[0]["id"]), ("user", None)]):
        parent, _ = await add_message(conv["id"], role, f"Line {index}", index, parent_id=parent, speaker_member_id=speaker)
    await set_active_leaf(conv["id"], parent)

    checkpoint = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()
    assert checkpoint["group_context_mode"] == "shared"

    response = await client.post(f"/api/conversations/{conv['id']}/compress", json={"summary": "So far.", "keep_count": 2})
    assert response.status_code == 200
    new_cid = response.json()["new_conversation_id"]
    forked = (await client.get("/api/conversations")).json()
    assert next(c for c in forked if c["id"] == new_cid)["group_context_mode"] == "shared"


async def test_shared_dossier_gives_every_speaker_one_prefix_and_never_repeats_identity(client, llm_mock):
    conv, _ = await _two_card_group(client, context_mode="shared")
    await _run_two_speaker_beat(client, llm_mock, conv)

    systems = _systems(llm_mock, "writer")
    assert len(systems) == 2
    # Best prefix sharing: both speakers read the identical cast dossier body.
    assert systems[0] == systems[1]
    for system in systems:
        assert system.count("## Character dossier: Aria") == 1
        assert system.count("## Character dossier: Kael") == 1
        assert "ARIA PRIVATE" in system and "KAEL PRIVATE" in system

    writers = [call for call in llm_mock.captured if call["pass"] == "writer"]
    # The identity fields are in the shared body, so the tail must not re-bill
    # them; the speaker-only guard stays.
    aria_tail = json.dumps(writers[0]["messages"][-1])
    assert "ARIA PRIVATE" not in aria_tail and "ARIA EXAMPLE" not in aria_tail
    assert "Write the next reply as Aria only" in aria_tail


async def test_private_perspective_keeps_the_cast_prefix_stable_and_cards_speaker_local(client, llm_mock):
    conv, _ = await _two_card_group(client)
    await _run_two_speaker_beat(client, llm_mock, conv)

    systems = _systems(llm_mock, "writer")
    assert systems[0] == systems[1]
    assert "ARIA PRIVATE" not in systems[0] and "KAEL PRIVATE" not in systems[0]
    writers = [call for call in llm_mock.captured if call["pass"] == "writer"]
    assert "ARIA PRIVATE" in json.dumps(writers[0]["messages"][-1])
    assert "ARIA PRIVATE" not in json.dumps(writers[1]["messages"][-1])


@pytest.mark.kv_divergence_expected
async def test_classic_card_swap_uses_a_neutral_director_base_and_one_prefix_per_speaker(client, llm_mock):
    """Swap's per-speaker prefix is a *deliberate* cache divergence — hence the
    marker. What must not happen is the Director seeing an arbitrary member's
    card, or the first planned speaker silently inheriting that neutral base."""
    conv, _ = await _two_card_group(client, context_mode="swap")
    await _run_two_speaker_beat(client, llm_mock, conv)

    director = _systems(llm_mock, "director")[0]
    assert "## Cast\nAria, Kael" in director
    assert "ARIA PRIVATE" not in director and "KAEL PRIVATE" not in director

    systems = _systems(llm_mock, "writer")
    assert len(systems) == 2
    # The `index == 0` shortcut would have handed speaker 1 the neutral base.
    assert systems[0] != director and systems[0] != systems[1]
    assert "ARIA PRIVATE" in systems[0] and "KAEL PRIVATE" not in systems[0]
    assert "KAEL PRIVATE" in systems[1] and "ARIA PRIVATE" not in systems[1]
    # Everything up to the active card is still shared with the neutral base.
    shared_head = director[: director.index("Aria, Kael") + len("Aria, Kael")]
    assert systems[0].startswith(shared_head) and systems[1].startswith(shared_head)


@pytest.mark.parametrize("mode", ["private", "shared", "swap"])
async def test_the_editor_replays_the_exact_writer_input_in_every_mode(client, llm_mock, mode):
    """The Editor must extend the Writer's stack, never rebuild its own view of
    the cast — otherwise it audits a draft written from a different prompt."""
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True, "editor_apply_patch": True},
            "length_guard_enabled": True,
            "length_guard_max_words": 5,
        },
    )
    conv, _ = await _two_card_group(client, context_mode=mode)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Look around"]))
    llm_mock.enqueue_writer("word " * 60)
    llm_mock.enqueue_editor(None)
    assert (await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "Go on"})).status_code == 200

    writer = next(call for call in llm_mock.captured if call["pass"] == "writer")
    editor = next((call for call in llm_mock.captured if call["pass"] == "editor"), None)
    assert editor is not None, "expected the editor to run"
    assert editor["messages"][: len(writer["messages"])] == writer["messages"]


async def _dual_model(client) -> None:
    """Put director/editor on their own endpoint, writer on the active one."""
    ep = await client.post("/api/endpoints", json={"url": "http://agent.local", "api_key": "k"})
    assert ep.status_code == 200
    response = await client.put(
        "/api/settings",
        json={
            "agent_same_as_writer": False,
            "agent_endpoint_id": ep.json()["id"],
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True, "editor_apply_patch": True},
            "length_guard_enabled": True,
            "length_guard_max_words": 5,
        },
    )
    assert response.status_code == 200


@pytest.mark.parametrize("mode", ["private", "shared"])
async def test_both_model_lanes_agree_on_the_cast_when_the_prefix_is_shared(client, llm_mock, mode):
    """The Editor's agent lane must see the same cast as the Writer it audits,
    and both must stay speaker-independent in the two shared-prefix modes."""
    await _dual_model(client)
    conv, _ = await _two_card_group(client, context_mode=mode)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail", "kael — Explain the ward"]))
    for _ in range(2):
        llm_mock.enqueue_writer("word " * 60)
        llm_mock.enqueue_editor(None)
    assert (await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})).status_code == 200

    writers, editors = _systems(llm_mock, "writer"), _systems(llm_mock, "editor")
    assert len(writers) == 2 and len(editors) == 2
    assert len(set(writers)) == 1, "writer prefix diverged across speakers"
    assert len(set(editors)) == 1, "agent prefix diverged across speakers"
    # Different system prompts per lane are expected; the *cast body* is not.
    body = "## Character dossier: Aria" if mode == "shared" else "### Aria"
    assert body in writers[0] and body in editors[0]


@pytest.mark.kv_divergence_expected
async def test_classic_card_swap_swaps_the_card_on_the_agent_lane_too(client, llm_mock):
    """Swap diverges both lanes per speaker (hence the marker) — but never
    unevenly: an Editor auditing Aria must not be reading Kael's card."""
    await _dual_model(client)
    conv, _ = await _two_card_group(client, context_mode="swap")
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail", "kael — Explain the ward"]))
    for _ in range(2):
        llm_mock.enqueue_writer("word " * 60)
        llm_mock.enqueue_editor(None)
    assert (await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})).status_code == 200

    for lane in (_systems(llm_mock, "writer"), _systems(llm_mock, "editor")):
        assert len(lane) == 2
        assert "ARIA PRIVATE" in lane[0] and "KAEL PRIVATE" not in lane[0]
        assert "KAEL PRIVATE" in lane[1] and "ARIA PRIVATE" not in lane[1]
    assert "ARIA PRIVATE" not in _systems(llm_mock, "director")[0]


@pytest.mark.parametrize("mode", ["private", "shared"])
async def test_the_post_turn_steps_ride_the_beat_base_rather_than_rebuilding_one(client, llm_mock, mode):
    """Dynamic Worlds and the direction-note step inherit the mode for free
    because they extend the speaker's frozen base. Asserted, not assumed: a
    step that rebuilt its own prefix would show up here as a second system
    message on the same lane."""
    world = (await client.post("/api/worlds", json={"name": "Gorge"})).json()
    await client.post(f"/api/worlds/{world['id']}/entries", json={"name": "Bridge", "content": "It groans.", "keywords": []})
    await client.put(f"/api/worlds/{world['id']}/dynamic", json={"enabled": True})
    await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})

    conv, _ = await _two_card_group(client, context_mode=mode)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Look around"]))
    llm_mock.enqueue_writer("The bridge gives way.")
    llm_mock.enqueue_world_change(
        [{"type": "function", "function": {"name": "propose_world_changes", "arguments": {"operations": []}}}]
    )
    assert (await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "Go on"})).status_code == 200

    proposal = [c for c in llm_mock.captured if c["pass"] not in ("director", "writer")]
    assert proposal, "expected the Dynamic Worlds step to run"
    writer = next(c for c in llm_mock.captured if c["pass"] == "writer")
    assert all(call["messages"][0] == writer["messages"][0] for call in proposal)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("private", ["cast_public", "largest_speaker_tail"]),
        ("shared", ["cast_dossiers", "largest_speaker_tail"]),
        ("swap", ["cast_names", "largest_active_card", "largest_speaker_tail"]),
    ],
)
async def test_context_size_breakdown_follows_the_context_mode(client, mode, expected):
    big = ("ARIA " * 40).strip()
    conv, _ = await _two_card_group(
        client,
        context_mode=mode,
        aria_extra={"description": big},
        kael_extra={"description": "KAEL"},
    )
    breakdown = (await client.get(f"/api/conversations/{conv['id']}/context-size")).json()["breakdown"]
    assert [key for key in expected if key in breakdown] == expected
    # Exactly one shared-body key per mode — a stale one would double-count.
    assert {"cast_public", "cast_dossiers", "cast_names"} & set(breakdown) == {expected[0]}
    # The biggest card is billed once wherever the mode puts it, never summed.
    billed = "largest_speaker_tail" if mode == "private" else ("largest_active_card" if mode == "swap" else "cast_dossiers")
    assert breakdown[billed]["chars"] >= len(big)
    assert breakdown["largest_speaker_tail"]["chars"] < len(big) or mode == "private"


@pytest.mark.parametrize("mode", ["private", "shared", "swap"])
async def test_compression_prompts_stay_on_the_public_cast_projection(client, llm_mock, mode):
    """Compression is scene-wide narration: paying for every dossier — or
    swapping in one arbitrary card — buys nothing on the app's longest call."""
    conv, members = await _two_card_group(client, context_mode=mode)
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
        parent, _ = await add_message(conv["id"], role, content, index, parent_id=parent, speaker_member_id=speaker)
    await set_active_leaf(conv["id"], parent)

    llm_mock.enqueue_writer("A summary.")
    assert (await client.post(f"/api/conversations/{conv['id']}/summarize", json={"keep_count": 2})).status_code == 200
    system = str(llm_mock.captured[-1]["messages"][0]["content"])
    assert "### Aria" in system and "### Kael" in system
    assert "ARIA PRIVATE" not in system and "KAEL PRIVATE" not in system
    assert "## Character dossier" not in system


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
