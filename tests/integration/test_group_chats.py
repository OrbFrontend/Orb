from __future__ import annotations

import json

import pytest

from backend.database import (
    add_message,
    add_phrase_group,
    get_interactive_fragments,
    get_messages,
    set_active_leaf,
    update_settings,
)


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


async def test_manual_group_without_a_pin_rests_instead_of_erroring(client, llm_mock):
    """`Choose` sends fine with nobody picked: the message lands, no one answers.

    The pick is a separate act (a cast chip), so a send that arrives without one
    is a rest, not a failed turn -- and a rest that is known before any prompt is
    built must not pay for a Director call.
    """
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
    events = _sse_events(response.text)
    assert not any(name == "error" for name, _ in events)
    plan = next(data for name, data in events if name == "speaking_plan")
    assert isinstance(plan, dict) and plan["plan"] == []
    assert llm_mock.calls == []
    messages = await get_messages(conv["id"])
    assert [(m["role"], m["content"]) for m in messages] == [("user", "Hello")]


async def test_manual_group_speaks_for_the_member_the_pin_names(client, llm_mock):
    aria = await _card(client, "Aria")
    kael = await _card(client, "Kael")
    conv = (
        await client.post(
            "/api/conversations",
            json={
                "kind": "group",
                "group_turn_mode": "manual",
                "members": [{"character_card_id": aria}, {"character_card_id": kael}],
            },
        )
    ).json()
    members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    llm_mock.enqueue_writer("Kael answers.")
    response = await client.post(
        f"/api/conversations/{conv['id']}/send",
        json={"content": "Hello", "speaker_member_id": members[1]["id"]},
    )
    assert response.status_code == 200
    plan = next(data for name, data in _sse_events(response.text) if name == "speaking_plan")
    assert [entry["name"] for entry in plan["plan"]] == ["Kael"]


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


# ── The scene-local sheet override ──────────────────────────────────────────
# `public_profile_override` is what the rest of the cast sees; `card_sheet_override`
# is what the member reads about *itself*. A card asserts turn one forever, so a
# long scene needs somewhere scene-local to say the coat burned — without writing
# the card, which stays a reusable shared asset.


async def _put_members(client, conv, members: list[dict]):
    response = await client.put(f"/api/conversations/{conv['id']}/members", json={"members": members})
    assert response.status_code == 200, response.text
    return response.json()


def _member_spec(member: dict, **overrides) -> dict:
    spec = {
        "id": member["id"],
        "character_card_id": member["character_card_id"],
        "display_name": member["display_name"],
        "public_profile_override": member.get("public_profile_override"),
        "card_sheet_override": member.get("card_sheet_override"),
        "member_kind": member["member_kind"],
        "muted": bool(member["muted"]),
    }
    return {**spec, **overrides}


@pytest.mark.parametrize("mode", ["private", "shared", "swap"])
async def test_the_sheet_override_is_stored_and_read_back_under_every_mode(client, mode):
    """Mode-blind, like `public_profile_override` and the scene-profile drafter:
    which modes *send* it is a UI concern, and a server that accepted it under
    one mode only would leave the two halves of one field disagreeing."""
    conv, members = await _two_card_group(client, context_mode=mode)
    updated = await _put_members(
        client,
        conv,
        [_member_spec(members[0], card_sheet_override="ARIA CURRENT SHEET"), _member_spec(members[1])],
    )
    assert updated[0]["card_sheet_override"] == "ARIA CURRENT SHEET"
    assert updated[1]["card_sheet_override"] is None


async def test_an_empty_sheet_override_blanks_the_sheet_rather_than_restoring_the_card(client, llm_mock):
    """`""` is a deliberate blanking and `null` is absence; the two must not
    collapse. Manage cast coerces an empty box to `null`, so today only the API
    reaches the blanking case — but the resolution rule is the server's, and
    collapsing them here would make the distinction unexpressible at all."""
    conv, members = await _two_card_group(client)
    updated = await _put_members(client, conv, [_member_spec(members[0], card_sheet_override=""), _member_spec(members[1])])
    assert updated[0]["card_sheet_override"] == ""

    await _run_two_speaker_beat(client, llm_mock, conv)
    writers = [call for call in llm_mock.captured if call["pass"] == "writer"]
    assert "ARIA PRIVATE" not in json.dumps(writers[0]["messages"][-1])
    # The other member is untouched: blanking is per-member, not per-scene.
    assert "KAEL PRIVATE" in json.dumps(writers[1]["messages"][-1])


async def test_a_private_sheet_override_moves_the_tail_and_leaves_the_cached_body_alone(client, llm_mock):
    """The point of the whole design. Under Private the speaker's own sheet sits
    *after* history, so keeping it current costs no prefix rebuild — the cached
    body must come back byte-identical across the edit."""
    conv, members = await _two_card_group(client)
    await _run_two_speaker_beat(client, llm_mock, conv)
    before = _systems(llm_mock, "writer")
    before_tail = json.dumps([call for call in llm_mock.captured if call["pass"] == "writer"][0]["messages"][-1])

    llm_mock.captured.clear()
    await _put_members(
        client,
        conv,
        [_member_spec(members[0], card_sheet_override="ARIA, hair shorn and coat burned."), _member_spec(members[1])],
    )
    await _run_two_speaker_beat(client, llm_mock, conv)
    after = _systems(llm_mock, "writer")
    after_tail = json.dumps([call for call in llm_mock.captured if call["pass"] == "writer"][0]["messages"][-1])

    assert after == before
    assert "ARIA PRIVATE" in before_tail and "hair shorn" not in before_tail
    assert "hair shorn" in after_tail and "ARIA PRIVATE" not in after_tail


@pytest.mark.parametrize("mode", ["shared", "swap"])
async def test_the_other_modes_read_the_override_too_even_though_it_lands_before_history(client, llm_mock, mode):
    """Shared and Swap put the sheet in the system body, so an edit there *is* a
    prefix rebuild. They still get the override — the field is mode-blind — but
    this is why the drift fix is aimed at Private."""
    conv, members = await _two_card_group(client, context_mode=mode)
    await _put_members(
        client,
        conv,
        [_member_spec(members[0], card_sheet_override="ARIA, hair shorn and coat burned."), _member_spec(members[1])],
    )
    await _run_two_speaker_beat(client, llm_mock, conv)
    systems = _systems(llm_mock, "writer")
    assert "hair shorn" in systems[0] and "ARIA PRIVATE" not in systems[0]


async def test_the_sheet_override_rides_checkpoint_and_compression_forks(client):
    """Asserted on the *copied* member ids: `create_group_conversation` re-mints
    them, so a fork that carried the text under the old ids would carry nothing."""
    conv, members = await _two_card_group(client)
    await _put_members(
        client,
        conv,
        [_member_spec(members[0], card_sheet_override="ARIA CURRENT SHEET"), _member_spec(members[1])],
    )
    parent = None
    for index, (role, speaker) in enumerate([("user", None), ("assistant", members[0]["id"]), ("user", None)]):
        parent, _ = await add_message(conv["id"], role, f"Line {index}", index, parent_id=parent, speaker_member_id=speaker)
    await set_active_leaf(conv["id"], parent)

    checkpoint = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()
    compressed = (
        await client.post(f"/api/conversations/{conv['id']}/compress", json={"summary": "So far.", "keep_count": 2})
    ).json()
    for cid in (checkpoint["id"], compressed["new_conversation_id"]):
        forked = (await client.get(f"/api/conversations/{cid}/members?include_inactive=true")).json()
        assert not {member["id"] for member in forked} & {member["id"] for member in members}
        assert [member["card_sheet_override"] for member in forked] == ["ARIA CURRENT SHEET", None]


async def test_compression_never_re_asserts_a_members_sheet_into_the_summary(client, llm_mock):
    """Compression forces the public-cast projection, which carries no sheet at
    all — neither the card's nor the override's. So a summary cannot re-assert
    pre-update appearance into a fork whose history no longer contradicts it,
    and the sheet needs no compression-side edit."""
    conv, members = await _two_card_group(client)
    await _put_members(
        client,
        conv,
        [_member_spec(members[0], card_sheet_override="ARIA SHEET OVERRIDE"), _member_spec(members[1])],
    )
    parent = None
    for index, (role, speaker) in enumerate([("user", None), ("assistant", members[0]["id"]), ("user", None), ("user", None)]):
        parent, _ = await add_message(conv["id"], role, f"Line {index}", index, parent_id=parent, speaker_member_id=speaker)
    await set_active_leaf(conv["id"], parent)

    llm_mock.enqueue_writer("A summary.")
    assert (await client.post(f"/api/conversations/{conv['id']}/summarize", json={"keep_count": 2})).status_code == 200
    system = str(llm_mock.captured[-1]["messages"][0]["content"])
    assert "ARIA SHEET OVERRIDE" not in system and "ARIA PRIVATE" not in system
    assert "### Aria" in system and "### Kael" in system


# ── The post-beat sheet-update pass ─────────────────────────────────────────
# One call per member the beat touched, staged pending, never applied. Routed
# through the mock's `workflow` queue for the reason `_profile_call` states: the
# schema is deliberately absent from `inference.tool_registry.TOOLS`.


def _sheet_call(**arguments) -> dict:
    """The forced ``update_character_sheet`` response."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "update_character_sheet", "arguments": arguments}}],
    }


async def _sheet_group(client, **kwargs) -> tuple[dict, list[dict]]:
    conv, members = await _two_card_group(client, **kwargs)
    updated = (await client.put(f"/api/conversations/{conv['id']}", json={"group_sheet_updates": True})).json()
    assert updated["group_sheet_updates"] == 1
    return updated, members


def _sheet_calls(llm_mock) -> list[str]:
    """Every sheet-update call's user message, in order."""
    return [
        str(call["messages"][-1]["content"])
        for call in llm_mock.captured
        if call["pass"] == "workflow" and "reference sheet" in str(call["messages"][-1]["content"])
    ]


async def _proposals(client, conv, status: str | None = None) -> list[dict]:
    """The scene's proposals. No status means the route's own default — the review
    set the client actually asks for, so the tests exercise the shipped call."""
    params = {"status": status} if status else {}
    response = await client.get(f"/api/conversations/{conv['id']}/sheet-proposals", params=params)
    assert response.status_code == 200
    return response.json()


async def test_the_sheet_pass_is_off_until_the_scene_opts_in(client, llm_mock):
    """One billed call per member a beat touched is not something a scene should
    start paying for by existing. Staleness is a property of a *long* scene."""
    conv, _ = await _two_card_group(client)
    assert conv["group_sheet_updates"] == 0
    await _run_two_speaker_beat(client, llm_mock, conv)
    assert _sheet_calls(llm_mock) == []
    assert await _proposals(client, conv) == []


async def test_an_opted_in_beat_stages_one_proposal_per_member_that_spoke(client, llm_mock):
    conv, members = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="ARIA, shorn and coatless.", summary="Cut her hair"))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)

    assert len(_sheet_calls(llm_mock)) == 2
    pending = await _proposals(client, conv)
    assert [(item["member_id"], item["proposed_sheet"], item["summary"]) for item in pending] == [
        (members[0]["id"], "ARIA, shorn and coatless.", "Cut her hair")
    ]
    # Staged only: the member's sheet is untouched until the user applies.
    reloaded = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    assert reloaded[0]["card_sheet_override"] is None


async def test_a_silent_member_is_never_asked_about(client, llm_mock):
    """Gated to the members the beat touched. Cast-wide would be one call per
    member per beat to be told nothing happened to them."""
    conv, members = await _sheet_group(client)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail"]))
    llm_mock.enqueue_writer("I found tracks.")
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    assert (await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})).status_code == 200

    calls = _sheet_calls(llm_mock)
    assert len(calls) == 1 and "Aria" in calls[0] and "Character: Kael" not in calls[0]


async def test_each_sheet_call_carries_only_its_own_members_sheet(client, llm_mock):
    """The never-batched rule, re-pinned for the new tool: another member's
    *prose* is shared evidence, their *sheet* is not."""
    conv, _ = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)

    aria_call, kael_call = _sheet_calls(llm_mock)
    assert "ARIA PRIVATE" in aria_call and "KAEL PRIVATE" not in aria_call
    assert "KAEL PRIVATE" in kael_call and "ARIA PRIVATE" not in kael_call
    # Both replies are in both calls: the transcript is the shared evidence.
    for call in (aria_call, kael_call):
        assert "I found tracks." in call and "The ward is broken." in call


async def test_the_pass_runs_once_per_beat_not_once_per_speaker(client, llm_mock):
    """Two speakers, two members, two calls — not four. `run_beat_final` is what
    makes the difference, and a regression here doubles the beat's bill."""
    conv, _ = await _sheet_group(client)
    for _ in range(4):
        llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)
    assert len(_sheet_calls(llm_mock)) == 2


async def test_applying_a_proposal_changes_the_tail_and_leaves_the_cached_body_alone(client, llm_mock):
    """The reason the sheet lives in the tail under Private. An applied update
    must cost no prefix rebuild, or keeping a scene current costs the KV cache."""
    conv, _ = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="ARIA, shorn and coatless.", summary="Cut her hair"))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)
    before = _systems(llm_mock, "writer")

    pending = await _proposals(client, conv)
    applied = await client.post(f"/api/conversations/{conv['id']}/sheet-proposals/{pending[0]['id']}/apply")
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    reloaded = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    assert reloaded[0]["card_sheet_override"] == "ARIA, shorn and coatless."

    llm_mock.captured.clear()
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)
    assert _systems(llm_mock, "writer") == before
    tail = json.dumps([call for call in llm_mock.captured if call["pass"] == "writer"][0]["messages"][-1])
    assert "shorn and coatless" in tail and "ARIA PRIVATE" not in tail


async def test_a_proposal_whose_sheet_moved_underneath_it_goes_stale_instead_of_clobbering(client, llm_mock):
    """The two-writer mitigation. `base_sheet` is to a proposal what
    `content_revision` is to a changeset — there is no force-apply here either."""
    conv, members = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="ARIA, shorn and coatless.", summary="Cut her hair"))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)
    pending = await _proposals(client, conv)

    await _put_members(
        client,
        conv,
        [_member_spec(members[0], card_sheet_override="ARIA, hand-edited."), _member_spec(members[1])],
    )
    response = await client.post(f"/api/conversations/{conv['id']}/sheet-proposals/{pending[0]['id']}/apply")
    assert response.status_code == 409
    assert (await _proposals(client, conv, "stale"))[0]["id"] == pending[0]["id"]
    # The hand edit stands: nothing was half-applied on the way to refusing.
    reloaded = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    assert reloaded[0]["card_sheet_override"] == "ARIA, hand-edited."


async def test_rejecting_writes_nothing_and_retires_the_row(client, llm_mock):
    conv, _ = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="ARIA, shorn and coatless.", summary="Cut her hair"))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)
    pending = await _proposals(client, conv)

    response = await client.post(f"/api/conversations/{conv['id']}/sheet-proposals/{pending[0]['id']}/reject")
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert await _proposals(client, conv) == []
    reloaded = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    assert reloaded[0]["card_sheet_override"] is None
    # A decided proposal is decided; a second apply cannot resurrect it.
    assert (await client.post(f"/api/conversations/{conv['id']}/sheet-proposals/{pending[0]['id']}/apply")).status_code == 409


async def test_a_second_beat_replaces_the_pending_proposal_instead_of_stacking_beside_it(client, llm_mock):
    """Two beats stage against the same stored sheet, so two pending proposals for
    one member are necessarily rivals: applying either makes the other 409. The
    later one replaces the earlier *in place*, and is built on its text, so the
    drift accumulates into one reviewable sheet instead of competing ones."""
    conv, members = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="ARIA, shorn.", summary="Cut her hair"))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)
    first = await _proposals(client, conv)
    assert len(first) == 1

    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="ARIA, shorn and coatless.", summary="Coat burned"))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)

    pending = await _proposals(client, conv)
    assert len(pending) == 1, "one member, one undecided proposal"
    assert pending[0]["id"] == first[0]["id"], "the row is rewritten, not replaced beside"
    assert pending[0]["proposed_sheet"] == "ARIA, shorn and coatless."
    # The second call reasoned from the first proposal, not from the stored sheet:
    # that is what makes replacing the row lossless.
    assert "ARIA, shorn." in _sheet_calls(llm_mock)[2]
    # `base_sheet` still names what an apply must match — the *stored* sheet.
    assert pending[0]["base_sheet"] == "ARIA PRIVATE"
    assert (await client.post(f"/api/conversations/{conv['id']}/sheet-proposals/{pending[0]['id']}/apply")).status_code == 200
    reloaded = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    assert reloaded[0]["card_sheet_override"] == "ARIA, shorn and coatless."
    assert members[0]["id"] == pending[0]["member_id"]


async def test_a_hand_edit_stops_the_carry_forward(client, llm_mock):
    """Carrying an undecided proposal forward must never resurrect text the user
    overwrote. A proposal whose base no longer matches the stored sheet is
    ignored, exactly as the apply refuses it."""
    conv, members = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="ARIA, shorn.", summary="Cut her hair"))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)

    await _put_members(
        client,
        conv,
        [_member_spec(members[0], card_sheet_override="ARIA, hand-edited."), _member_spec(members[1])],
    )
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)

    third = _sheet_calls(llm_mock)[2]
    assert "ARIA, hand-edited." in third and "ARIA, shorn." not in third


async def test_the_review_set_keeps_the_refusal_it_owes_an_explanation_for(client, llm_mock):
    """A `stale` proposal is one the apply refused. Listing only `pending` made it
    vanish at the moment of refusal, which is the opposite of reporting it — and
    left the review surface's own stale row unreachable."""
    conv, members = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="ARIA, shorn.", summary="Cut her hair"))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)
    staged = (await _proposals(client, conv))[0]

    await _put_members(
        client,
        conv,
        [_member_spec(members[0], card_sheet_override="ARIA, hand-edited."), _member_spec(members[1])],
    )
    assert (await client.post(f"/api/conversations/{conv['id']}/sheet-proposals/{staged['id']}/apply")).status_code == 409

    review = await _proposals(client, conv)
    assert [(item["id"], item["status"]) for item in review] == [(staged["id"], "stale")]
    # And it can still be cleared, which is the only action left on it.
    assert (await client.post(f"/api/conversations/{conv['id']}/sheet-proposals/{staged['id']}/reject")).status_code == 200
    assert await _proposals(client, conv) == []


async def test_removing_a_member_retires_its_undecided_proposals(client, llm_mock):
    """Manage cast renders rows only for the active roster, so a proposal left
    pending on a tombstoned member sat in the review count forever with no row to
    dismiss it from — and the apply, which has no active sheet to write onto,
    could only refuse."""
    conv, members = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="ARIA, shorn.", summary="Cut her hair"))
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_beat(client, llm_mock, conv)
    staged = (await _proposals(client, conv))[0]

    await _put_members(client, conv, [_member_spec(members[1])])
    assert await _proposals(client, conv) == []
    assert (await _proposals(client, conv, "all"))[0]["status"] == "rejected"
    assert (await client.post(f"/api/conversations/{conv['id']}/sheet-proposals/{staged['id']}/apply")).status_code == 409


async def test_the_pass_is_gated_on_the_mode_by_the_server_not_only_by_the_form(client, llm_mock):
    """The opt-in is priced on Private perspective: only there does a member's
    sheet ride the uncached tail, so only there does keeping it current cost no
    prefix rebuild. Under Shared the same text sits in the cached body and an
    applied update rebuilds the whole scene prefix — so a `PUT` that changes only
    the mode must stop the pass, not leave it running against a layout it was
    never priced for."""
    conv, _ = await _sheet_group(client)
    updated = (await client.put(f"/api/conversations/{conv['id']}", json={"group_context_mode": "shared"})).json()
    assert updated["group_context_mode"] == "shared" and updated["group_sheet_updates"] == 1

    await _run_two_speaker_beat(client, llm_mock, updated)
    assert _sheet_calls(llm_mock) == []
    assert await _proposals(client, updated) == []


async def test_a_chip_click_reads_the_round_rather_than_its_own_request(client, llm_mock):
    """A beat is request-scoped, so under Manual — and for any cast-chip click on
    a resting scene — one round is several requests. Judging "did this beat change
    Kael?" from Kael's reply alone leaves out the line that changed him, and on
    `/speak` leaves out the user's message entirely. The evidence is the round,
    which is what `image_gen`'s subject list already reads."""
    conv, members = await _sheet_group(client)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail"]))
    llm_mock.enqueue_writer("I found tracks.")
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    assert (await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})).status_code == 200

    llm_mock.enqueue_writer("The ward is broken.")
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    response = await client.post(f"/api/conversations/{conv['id']}/speak", json={"speaker_member_id": members[1]["id"]})
    assert response.status_code == 200

    calls = _sheet_calls(llm_mock)
    assert len(calls) == 2
    # Kael's call is about Kael, but it reads the whole round: the user's message
    # and Aria's reply, neither of which was part of Kael's own request.
    assert "Character: Kael" in calls[1]
    for evidence in ("What happened?", "I found tracks.", "The ward is broken."):
        assert evidence in calls[1]
    # Only this request's speaker is proposed *about*: Aria's own call was already
    # billed by the request she spoke in.
    assert "Character: Aria" not in calls[1]


async def test_a_failed_sheet_call_never_costs_the_user_their_reply(client, llm_mock):
    """It is the last step before `_result` and it is bookkeeping. A malformed
    answer drops the proposal and nothing else."""
    conv, _ = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="Has a {{char}} macro in it."))
    llm_mock.enqueue_workflow(_sheet_call(changed=True))  # reports a change, returns no sheet
    await _run_two_speaker_beat(client, llm_mock, conv)

    assert await _proposals(client, conv) == []
    rows = await get_messages(conv["id"])
    assert [row["content"] for row in rows if row["role"] == "assistant"] == ["I found tracks.", "The ward is broken."]


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


async def _enqueue_per_fragment_director(llm_mock, **arguments) -> None:
    """Queue one director response per step of the per-fragment loop.

    That mode runs one forced call per interactive fragment, then one for the
    speaking plan, then one for moods — and each step keeps only its own target
    field, so handing every step the same arguments is safe and saves the test
    from counting the seeded fragments.
    """
    fragments = [f for f in await get_interactive_fragments() if f.get("enabled", True)]
    for _ in range(len(fragments) + 2):
        llm_mock.enqueue_director(_direct_scene(**arguments))


async def test_per_fragment_director_still_plans_a_group_beat(client, llm_mock):
    """`director_individual_fragments` runs each direct_scene field in its own
    forced call. The speaking plan is one of those fields, so it has to survive
    that loop — it used to take the whole group turn down with it."""
    await update_settings({"director_individual_fragments": 1})
    conv, members = await _two_card_group(client)
    await _enqueue_per_fragment_director(llm_mock, speaking_plan=["kael — Answer first"])
    llm_mock.enqueue_writer("The ward is broken.")

    response = await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})
    assert response.status_code == 200
    events = _sse_events(response.text)
    assert not [data for name, data in events if name == "error"]
    plan = next(data for name, data in events if name == "speaking_plan")
    assert [item["member_id"] for item in plan["plan"]] == [members[1]["id"]]


async def test_an_intentional_rest_survives_both_director_shapes(client, llm_mock):
    """`[]` is the Director saying nobody answers this beat. The per-fragment loop
    drops empty values by design, so it has to make an exception for the plan or a
    rest silently becomes a round-robin reply."""
    for individual in (0, 1):
        await update_settings({"director_individual_fragments": individual})
        conv, _ = await _two_card_group(client)
        if individual:
            await _enqueue_per_fragment_director(llm_mock, speaking_plan=[])
        else:
            llm_mock.enqueue_director(_direct_scene(speaking_plan=[]))

        response = await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "Nobody move."})
        assert response.status_code == 200
        plan = next(data for name, data in _sse_events(response.text) if name == "speaking_plan")
        assert plan["plan"] == [], f"director_individual_fragments={individual} overrode the rest"
        assert [m["role"] for m in await get_messages(conv["id"])] == ["user"]


async def test_a_missing_plan_falls_back_rather_than_resting(client, llm_mock):
    """The rest exception is for an explicit `[]` only — a Director that never
    filled the field at all must still get the configured strategy."""
    await update_settings({"director_individual_fragments": 1})
    conv, members = await _two_card_group(client)
    await _enqueue_per_fragment_director(llm_mock, moods=[])
    llm_mock.enqueue_writer("I found tracks.")

    response = await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})
    assert response.status_code == 200
    plan = next(data for name, data in _sse_events(response.text) if name == "speaking_plan")
    assert [item["member_id"] for item in plan["plan"]] == [members[0]["id"]]


async def test_group_steering_excludes_the_reply_it_replaces_from_the_audit(client, llm_mock):
    """The steered paths hand the editor an explicit baseline window so the new
    draft is not penalised for resembling the reply being replaced. A group beat
    has to receive the same list, or the target rides the prefix into the window
    and the anti-echo audit scores the draft against itself.

    Driven through the structural-repetition scanner: an identical draft is a
    finding when its twin is in the window and silence when it is not, so the
    editor firing at all is the observable.
    """
    # The editor only runs with the Agent on, its patch tool enabled and a phrase
    # bank present; without all three `audit_enabled` is False and nothing is scanned.
    await update_settings({"enable_agent": 1, "enabled_tools": {"direct_scene": True, "editor_apply_patch": True}})
    await add_phrase_group(["a sharp intake of breath"])
    twin = "She crossed the yard, counted the lamps, and stopped at the gate."

    async def _steer(*, older: str, replaced: str) -> list[dict]:
        conv, members = await _two_card_group(client)
        first_user, _ = await add_message(conv["id"], "user", "Then?", 0, beat_id="b0")
        older_id, _ = await add_message(
            conv["id"], "assistant", older, 1, parent_id=first_user, speaker_member_id=members[0]["id"], beat_id="b0"
        )
        user_id, _ = await add_message(conv["id"], "user", "And after?", 2, parent_id=older_id, beat_id="b1")
        target, _ = await add_message(
            conv["id"], "assistant", replaced, 3, parent_id=user_id, speaker_member_id=members[0]["id"], beat_id="b1"
        )
        await set_active_leaf(conv["id"], target)
        llm_mock.captured.clear()
        llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Try again"]))
        llm_mock.enqueue_writer(twin)
        response = await client.post(f"/api/conversations/{conv['id']}/messages/{target}/super_regenerate", json={})
        assert response.status_code == 200
        return [call for call in llm_mock.captured if call["pass"] == "editor"]

    # Positive control: an older reply on the branch stays in the window, so an
    # identical draft is caught. Without this the assertion below proves nothing.
    assert await _steer(older=twin, replaced="Kael shrugged and said nothing at all."), (
        "the structural scanner never fired, so the exclusion below is untested"
    )
    assert not await _steer(older="Kael shrugged and said nothing at all.", replaced=twin), (
        "the reply being replaced was audited against its own replacement"
    )


# ── Scene-profile drafting ──────────────────────────────────────────────────
# The generator behind Manage cast's Draft / Redraft buttons. One LLM call per
# member, never batched -- the leak that batching would open is pinned below.


def _profile_call(**arguments) -> dict:
    """The forced ``draft_public_profile`` response.

    ``_pass_from_tool_choice`` routes any forced tool name it does not recognise
    as a core pass tool to the ``workflow`` queue, and this schema is
    deliberately absent from ``inference.tool_registry.TOOLS``.
    """
    return {"tool_calls": [{"type": "function", "function": {"name": "draft_public_profile", "arguments": arguments}}]}


def _drafting_message(llm_mock) -> str:
    return str(llm_mock.captured[-1]["messages"][-1]["content"])


async def _draft(client, cid: str, **body):
    return await client.post(f"/api/conversations/{cid}/members/scene-profile/generate", json=body)


async def test_scene_profile_draft_renders_the_two_liner(client, llm_mock):
    """The same shape `_public_profile()` renders from a card, so an overridden
    member and a non-overridden one read identically in the assembled prompt.
    Nothing is persisted — Save cast is still what writes it."""
    conv, members = await _two_card_group(client)
    llm_mock.enqueue_workflow(_profile_call(appearance="Tall, in road-worn green.", role="Scout of the watch."))

    response = await _draft(client, conv["id"], character_card_id=members[0]["character_card_id"], display_name="Aria")
    assert response.status_code == 200
    assert response.json() == {"profile": "Appearance: Tall, in road-worn green.\nRole: Scout of the watch."}

    reloaded = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    assert reloaded[0]["public_profile_override"] is None


async def test_scene_profile_draft_sends_only_the_target_card_and_other_names(client, llm_mock):
    """The executable form of the no-batching decision.

    Kael's card must never enter Aria's drafting context: the result is a string
    every member reads under Private perspective, so a leak here writes his
    secret into the one place that mode promises it cannot appear. A future
    "batch the whole cast for speed" optimisation has to break loudly here.
    """
    conv, members = await _two_card_group(client, kael_extra={"description": "KAEL SECRET", "personality": "KAEL INNER"})
    llm_mock.enqueue_workflow(_profile_call(appearance="Tall.", role="Scout."))

    response = await _draft(
        client,
        conv["id"],
        character_card_id=members[0]["character_card_id"],
        display_name="Aria",
        cast_names=["Kael"],
    )
    assert response.status_code == 200
    sent = _drafting_message(llm_mock)
    assert "KAEL SECRET" not in sent and "KAEL INNER" not in sent and "KAEL EXAMPLE" not in sent
    assert "Kael" in sent  # the name, and only the name
    assert "ARIA PRIVATE" in sent  # the target's own card is the whole material


async def test_scene_profile_draft_carries_the_scene_premise(client, llm_mock):
    """The premise comes from the server, not the modal — it is durable scene
    configuration, and the client never gets to say what the scene is."""
    conv, members = await _two_card_group(client)
    await client.put(f"/api/conversations/{conv['id']}", json={"character_scenario": "A cold night on the wall."})
    llm_mock.enqueue_workflow(_profile_call(appearance="Tall.", role="Scout."))

    await _draft(client, conv["id"], character_card_id=members[0]["character_card_id"])
    assert "A cold night on the wall." in _drafting_message(llm_mock)


async def test_scene_profile_draft_seeds_the_card_level_profile_as_the_default(client, llm_mock):
    """A card-level profile is what this scene's override replaces, so the model
    is asked to adjust it rather than to invent a second one from scratch."""
    conv, members = await _two_card_group(client)
    card_id = members[0]["character_card_id"]
    saved = await client.put(
        f"/api/characters/{card_id}/public-profile",
        json={"appearance": "Green cloak, longbow.", "role": "Ranger."},
    )
    assert saved.status_code == 200
    llm_mock.enqueue_workflow(_profile_call(appearance="Tall.", role="Scout."))

    await _draft(client, conv["id"], character_card_id=card_id)
    sent = _drafting_message(llm_mock)
    assert "Appearance: Green cloak, longbow.\nRole: Ranger." in sent
    assert "the default this scene's profile replaces" in sent
    assert "Adjust that default" in sent


async def test_scene_profile_draft_for_a_cardless_member_is_a_sentence_not_a_422(client, llm_mock):
    conv, _ = await _two_card_group(client)
    response = await _draft(client, conv["id"], display_name="Narrator")
    assert response.status_code == 400
    # A prose `detail`, not FastAPI's validation list, and it names what went wrong.
    # The sentence itself is not pinned -- rewording it is not a regression.
    detail = response.json()["detail"]
    assert isinstance(detail, str) and "narrator" in detail.lower()


async def test_scene_profile_draft_on_a_solo_conversation_is_409(client, llm_mock):
    card_id = await _card(client, "Solo")
    conv = (await client.post("/api/conversations", json={"character_card_id": card_id})).json()
    response = await _draft(client, conv["id"], character_card_id=card_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "Conversation is not a group"


async def test_scene_profile_draft_on_a_missing_card_is_404(client, llm_mock):
    conv, _ = await _two_card_group(client)
    response = await _draft(client, conv["id"], character_card_id="no-such-card")
    assert response.status_code == 404


async def test_scene_profile_draft_works_for_a_row_not_yet_on_the_roster(client, llm_mock):
    """Manage cast is client-side until Save, so a member added seconds ago
    exists only in the DOM. Drafting for it must not require a roster row."""
    conv, members = await _two_card_group(client)
    newcomer = await _card(client, "Mira", description="MIRA PRIVATE")
    llm_mock.enqueue_workflow(_profile_call(appearance="Small, quick.", role="Thief."))

    response = await _draft(client, conv["id"], character_card_id=newcomer, display_name="Mira", cast_names=["Aria", "Kael"])
    assert response.status_code == 200
    assert response.json()["profile"] == "Appearance: Small, quick.\nRole: Thief."
    assert "MIRA PRIVATE" in _drafting_message(llm_mock)
    assert members  # the roster is untouched by a draft


async def test_omitted_cast_names_fall_back_to_the_stored_roster_without_the_target(client, llm_mock):
    conv, members = await _two_card_group(client)
    llm_mock.enqueue_workflow(_profile_call(appearance="Tall.", role="Scout."))

    await _draft(client, conv["id"], character_card_id=members[0]["character_card_id"])
    sent = _drafting_message(llm_mock)
    assert "Kael" in sent
    # Aria is the target; she is named as the subject, never as an other member.
    assert "names only)" in sent
    others = sent.split("names only):")[1].split('"""')[1]
    assert "Aria" not in others


async def test_client_supplied_names_keep_ui_order_and_duplicates(client, llm_mock):
    """Untrusted display text — stripped and capped, but never sorted or
    deduplicated: canonical order is what the user sees, and two members may
    legitimately share one display name."""
    conv, members = await _two_card_group(client)
    llm_mock.enqueue_workflow(_profile_call(appearance="Tall.", role="Scout."))

    await _draft(
        client,
        conv["id"],
        character_card_id=members[0]["character_card_id"],
        cast_names=["  Zed  ", "Kael", "Zed", "   ", "K" * 100],
    )
    sent = _drafting_message(llm_mock)
    assert f"Zed, Kael, Zed, {'K' * 64}" in sent


async def test_a_large_cast_is_bounded_and_says_how_many_it_left_out(client, llm_mock):
    """A prompt-size guard, not a roster limit. The roster has no ceiling, so the
    prompt must not claim the list it carries is the whole cast."""
    conv, members = await _two_card_group(client)
    llm_mock.enqueue_workflow(_profile_call(appearance="Tall.", role="Scout."))

    await _draft(
        client,
        conv["id"],
        character_card_id=members[0]["character_card_id"],
        cast_names=[f"Extra{i}" for i in range(17)],
    )
    sent = _drafting_message(llm_mock)
    assert "Extra15" in sent and "Extra16" not in sent
    assert "Other cast members omitted from this draft: 1" in sent


async def test_scene_profile_draft_is_mode_blind(client, llm_mock):
    """`PUT …/members` accepts an override under every mode, so a generate route
    that refused would leave the two halves of one field disagreeing about
    whether the mode is a server rule. Gating is the UI's job."""
    for mode in ("private", "shared", "swap"):
        conv, members = await _two_card_group(client, context_mode=mode)
        llm_mock.enqueue_workflow(_profile_call(appearance="Tall.", role="Scout."))
        response = await _draft(client, conv["id"], character_card_id=members[0]["character_card_id"])
        assert response.status_code == 200, mode


async def test_scene_profile_draft_without_a_tool_call_is_502(client, llm_mock):
    """A loop that writes N overrides the user saves in one click cannot degrade
    silently — a fabricated draft is indistinguishable from a real one."""
    conv, members = await _two_card_group(client)
    llm_mock.enqueue_workflow({"role": "assistant", "content": "I'm not sure who that is."})

    response = await _draft(client, conv["id"], character_card_id=members[0]["character_card_id"])
    assert response.status_code == 502
    assert response.json()["detail"] == "The model did not return a usable profile."


@pytest.mark.parametrize(
    ("arguments", "why"),
    [
        ({"appearance": "As tall as {{user}}.", "role": "Scout."}, "macro"),
        ({"appearance": "", "role": "Scout."}, "empty"),
        ({"appearance": " ".join(f"w{i}" for i in range(31)), "role": "Scout."}, "longer than 30 words"),
    ],
)
async def test_a_draft_that_fails_the_output_contract_is_502_with_no_payload(client, llm_mock, arguments, why):
    conv, members = await _two_card_group(client)
    llm_mock.enqueue_workflow(_profile_call(**arguments))

    response = await _draft(client, conv["id"], character_card_id=members[0]["character_card_id"])
    assert response.status_code == 502
    assert why in response.json()["detail"]
    assert "profile" not in response.json()


async def test_a_checkpoint_carries_the_scenes_sheet_update_opt_in(client, llm_mock):
    """Every other scene setting rides `fork_conversation`; this one was dropped.

    Checkpoint, Compress History and "New scene in this group" all fork, so a user
    who turned the post-beat pass on lost it the first time they branched — and
    silently, since nothing reports a setting reverting to its default.
    """
    conv, _ = await _sheet_group(client)
    response = await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={"title": "Checkpoint"})
    assert response.status_code == 200, response.text
    assert response.json()["group_sheet_updates"] == 1

    # The same for a fresh scene in the family, which forks the same way.
    fresh = await client.post(f"/api/conversations/{conv['id']}/group-conversation")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["group_sheet_updates"] == 1
