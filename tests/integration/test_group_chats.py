import json
from os.path import commonprefix

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
    return await client.create("/api/characters", json={"name": name, **extra})


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


def _plan(response) -> dict:
    return next(data for name, data in _sse_events(response.text) if name == "speaking_plan")  # type: ignore[return-value]


def _writers(llm_mock) -> list[dict]:
    return [call for call in llm_mock.captured if call["pass"] == "writer"]


async def _members(client, cid: str, query: str = "") -> list[dict]:
    return await client.get_json(f"/api/conversations/{cid}/members{query}")


async def _scene(client, cards: list[str], **fields) -> tuple[dict, list[dict]]:
    members = [{"character_card_id": card} for card in cards]
    conv = await client.post_json("/api/conversations", json={"kind": "group", **fields, "members": members})
    return conv, await _members(client, conv["id"])


async def _group(client, *names: str, **fields) -> tuple[dict, list[dict]]:
    return await _scene(client, [await _card(client, name) for name in names], **fields)


async def _chain(cid: str, rows) -> list[int]:
    """Add ``(role, content, speaker[, exchange_id])`` rows as one branch and make its tip active."""
    ids: list[int] = []
    for index, (role, content, speaker, *exchange) in enumerate(rows):
        message_id, _ = await add_message(
            cid,
            role,
            content,
            index,
            parent_id=ids[-1] if ids else None,
            speaker_member_id=speaker,
            exchange_id=exchange[0] if exchange else None,
        )
        ids.append(message_id)
    await set_active_leaf(cid, ids[-1])
    return ids


async def _send(client, cid: str, content: str):
    return await client.post_checked(f"/api/conversations/{cid}/send", json={"content": content})


async def _latest_child(db, parent_id: int):
    return await db.one("SELECT * FROM messages WHERE parent_id = ? ORDER BY id DESC LIMIT 1", (parent_id,))


async def test_group_creation_allocates_durable_members(client, db):
    conv, members = await _group(client, "Aria", "Kael", title="Campfire", group_turn_mode="round_robin", group_max_speakers=2)
    assert conv["kind"] == "group"
    assert conv["character_card_id"] is None
    assert [(m["speaker_key"], m["display_name"]) for m in members] == [("aria", "Aria"), ("kael", "Kael")]
    assert len({m["id"] for m in members}) == 2


async def test_group_list_includes_active_cast_names_in_roster_order(client, db):
    conv, _ = await _group(client, "Aria", "Kael", title="Campfire")
    row = next(item for item in await client.get_json("/api/conversations") if item["id"] == conv["id"])
    assert row["group_member_names"] == ["Aria", "Kael"]


async def test_conversion_stamps_existing_assistant_identity(client, db):
    card_id = await _card(client, "Solo")
    conv = await client.post_json("/api/conversations", json={"character_card_id": card_id})
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content, turn_index, created_at) VALUES (?, 'assistant', 'hello', 0, 'now')",
        (conv["id"],),
    )
    await db.commit()
    response = await client.post_json(f"/api/conversations/{conv['id']}/convert-to-group")
    row = await db.one("SELECT speaker_member_id FROM messages WHERE conversation_id = ?", (conv["id"],))
    assert row["speaker_member_id"] == response["member"]["id"]


async def test_roster_removal_tombstones_and_readd_gets_new_identity(client):
    conv, [original] = await _group(client, "Echo")
    narrator = {"display_name": "Narrator", "member_kind": "narrator"}
    await client.put_checked(f"/api/conversations/{conv['id']}/members", json={"members": [narrator]})
    response = await client.put_json(
        f"/api/conversations/{conv['id']}/members",
        json={"members": [narrator, {"character_card_id": original["character_card_id"]}]},
    )
    readded = next(member for member in response if member["character_card_id"] == original["character_card_id"])
    assert readded["id"] != original["id"]
    history = await _members(client, conv["id"], "?include_inactive=true")
    assert next(member for member in history if member["id"] == original["id"])["active"] == 0


async def test_public_profile_merge_preserves_other_orb_extensions(client):
    card_id = await _card(
        client, "Profiled", extensions={"orb": {"fragments": {"mood": []}, "v3": {"nickname": "P"}}, "vendor": {"x": 1}}
    )
    await client.put_checked(f"/api/characters/{card_id}/public-profile", json={"appearance": "Silver hair", "role": "Scout"})
    card = await client.get_json(f"/api/characters/{card_id}")
    assert card["extensions"]["orb"]["public_profile"] == {"appearance": "Silver hair", "role": "Scout"}
    assert card["extensions"]["orb"]["v3"] == {"nickname": "P"}
    assert card["extensions"]["vendor"] == {"x": 1}


async def test_director_group_exchange_streams_and_persists_an_ordered_message_chain(client, llm_mock):
    conv, members = await _two_card_group(client)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail", "kael — Explain the ward"]))
    llm_mock.enqueue_writer("**Aria:**\nI found tracks.")
    llm_mock.enqueue_writer("Kael: The ward is broken.")

    names = [name for name, _ in _sse_events((await _send(client, conv["id"], "What happened?")).text)]
    assert names.count("speaking_plan") == 1
    assert names.count("speaker_start") == 2
    assert names.count("speaker_done") == 2
    assert names[-1] == "done"

    user, first, second = (await get_messages(conv["id"]))[-3:]
    assert [first["speaker_member_id"], second["speaker_member_id"]] == [members[0]["id"], members[1]["id"]]
    assert first["content"] == "I found tracks."
    assert second["content"] == "The ward is broken."
    assert first["parent_id"] == user["id"] and second["parent_id"] == first["id"]
    assert user["exchange_id"] == first["exchange_id"] == second["exchange_id"]

    writers = _writers(llm_mock)
    assert "ARIA PRIVATE" in json.dumps(writers[0]["messages"])
    assert "KAEL PRIVATE" not in json.dumps(writers[0]["messages"])
    assert "KAEL PRIVATE" in json.dumps(writers[1]["messages"])


async def test_every_speaker_in_an_exchange_sees_the_user_s_image(client, llm_mock):
    """An upload answers the whole cast; later speakers only see it through the replayed user row, so the row carries it."""
    conv, _ = await _group(client, "Aria", "Kael", title="Campfire")
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Look", "kael — Look too"]))
    llm_mock.enqueue_writer("Aria speaks.")
    llm_mock.enqueue_writer("Kael speaks.")

    pixel = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNiAAAABgADNjd8qAAAAABJRU5ErkJggg=="
    await client.post_checked(
        f"/api/conversations/{conv['id']}/send",
        json={"content": "What is this?", "attachments": [{"b64": pixel, "mime": "image/png", "filename": "map.png"}]},
    )
    writers = _writers(llm_mock)
    assert len(writers) == 2
    for writer in writers:
        assert pixel in json.dumps(writer["messages"]), "a speaker was asked about an image it never saw"


async def test_manual_group_without_a_pin_rests_instead_of_erroring(client, llm_mock):
    """`Choose` sends fine with nobody picked: the message lands, no one answers, and no Director call is paid for."""
    conv, _ = await _group(client, "Aria", group_turn_mode="manual")
    response = await _send(client, conv["id"], "Hello")
    assert not any(name == "error" for name, _ in _sse_events(response.text))
    plan = _plan(response)
    assert isinstance(plan, dict) and plan["plan"] == []
    assert llm_mock.calls == []
    assert [(m["role"], m["content"]) for m in await get_messages(conv["id"])] == [("user", "Hello")]


async def test_manual_group_speaks_for_the_member_the_pin_names(client, llm_mock):
    conv, members = await _group(client, "Aria", "Kael", group_turn_mode="manual")
    llm_mock.enqueue_writer("Kael answers.")
    response = await client.post_checked(
        f"/api/conversations/{conv['id']}/send", json={"content": "Hello", "speaker_member_id": members[1]["id"]}
    )
    assert [entry["name"] for entry in _plan(response)["plan"]] == ["Kael"]


async def test_atomic_roster_sync_allows_cards_to_swap_existing_member_slots(client):
    conv, members = await _group(client, "Aria", "Kael")
    aria, kael = (member["character_card_id"] for member in members)
    response = await client.put_json(
        f"/api/conversations/{conv['id']}/members",
        json={"members": [{**members[0], "character_card_id": kael}, {**members[1], "character_card_id": aria}]},
    )
    assert [member["character_card_id"] for member in response] == [kael, aria]


async def test_group_compress_remaps_speaker_ids_and_preserves_exchange_ids(client):
    conv, old_members = await _group(client, "Aria", "Kael")
    exchange_id = "exchange-copy"
    await _chain(
        conv["id"],
        [
            ("user", "Question", None, exchange_id),
            ("assistant", "Aria reply", old_members[0]["id"], exchange_id),
            ("assistant", "Kael reply", old_members[1]["id"], exchange_id),
        ],
    )
    response = await client.post_json(
        f"/api/conversations/{conv['id']}/compress",
        json={"summary": "Summary with Aria and Kael attribution.", "keep_count": 2},
    )
    new_cid = response["new_conversation_id"]
    new_members = await _members(client, new_cid, "?include_inactive=true")
    rows = await get_messages(new_cid)
    assert rows[0]["speaker_member_id"] is None
    assert rows[0]["content"].startswith("Summary with Aria")
    assert [row["exchange_id"] for row in rows[1:]] == [exchange_id, exchange_id]
    assert [row["speaker_member_id"] for row in rows[1:]] == [new_members[0]["id"], new_members[1]["id"]]
    assert not {row["speaker_member_id"] for row in rows[1:]} & {member["id"] for member in old_members}


def _five_lines(members: list[dict]) -> list[tuple]:
    aria, kael = members[0]["id"], members[1]["id"]
    rows = [("user", "One", None), ("assistant", "Two", aria), ("assistant", "Three", kael), ("user", "Four", None)]
    return [(*row, f"exchange-{index}") for index, row in enumerate([*rows, ("assistant", "Five", aria)])]


async def test_group_summarize_labels_history_and_context_size_is_a_maximum(client, llm_mock):
    big = "KAEL LARGEST PRIVATE SHEET " * 10
    cards = [await _card(client, "Aria", description="short private"), await _card(client, "Kael", description=big)]
    conv, members = await _scene(client, cards)
    await _chain(conv["id"], _five_lines(members))
    llm_mock.enqueue_writer("A summary.")
    await client.post_checked(f"/api/conversations/{conv['id']}/summarize", json={"keep_count": 2})
    prompt = json.dumps(llm_mock.captured[-1]["messages"])
    assert "Aria: Two" in prompt and "Kael: Three" in prompt

    context = await client.get_json(f"/api/conversations/{conv['id']}/context-size")
    assert context["estimate_kind"] == "maximum"
    assert context["breakdown"]["largest_speaker_tail"]["chars"] >= len(big.strip())


async def test_summarizing_a_renamed_group_calls_it_by_its_current_name(client, llm_mock):
    """`{{char}}` is the scene's editable title in a group; `character_name` keeps the founding name."""
    conv, _ = await _group(client, "Aria", title="Campfire")
    await client.put(
        f"/api/conversations/{conv['id']}", json={"title": "The Long Watch", "character_scenario": "{{char}} opens at dusk."}
    )
    await _chain(
        conv["id"], [(role, text, None) for role, text in zip(["user", "assistant"] * 2, ["One", "Two", "Three", "Four"])]
    )
    llm_mock.enqueue_writer("A summary.")

    await client.post_checked(f"/api/conversations/{conv['id']}/summarize", json={"keep_count": 2})
    prompt = json.dumps(llm_mock.captured[-1]["messages"])
    assert "The Long Watch opens at dusk." in prompt
    assert "Campfire" not in prompt


# -- Character context modes -------------------------------------------------


async def _two_card_group(
    client, *, context_mode: str | None = None, aria_extra: dict | None = None, kael_extra: dict | None = None
) -> tuple[dict, list[dict]]:
    aria = await _card(client, "Aria", **{"description": "ARIA PRIVATE", "mes_example": "ARIA EXAMPLE", **(aria_extra or {})})
    kael = await _card(client, "Kael", **{"description": "KAEL PRIVATE", "mes_example": "KAEL EXAMPLE", **(kael_extra or {})})
    conv, members = await _scene(client, [aria, kael], title="Campfire")
    if context_mode:
        conv = await client.put_json(f"/api/conversations/{conv['id']}", json={"group_context_mode": context_mode})
    return conv, members


async def _run_two_speaker_exchange(client, llm_mock, conv):
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail", "kael — Explain the ward"]))
    llm_mock.enqueue_writer("I found tracks.")
    llm_mock.enqueue_writer("The ward is broken.")
    return await _send(client, conv["id"], "What happened?")


def _systems(llm_mock, pass_name: str) -> list[str]:
    return [str(call["messages"][0]["content"]) for call in llm_mock.captured if call["pass"] == pass_name]


async def _three_lines(conv: dict, members: list[dict], extra_user: bool = False) -> None:
    rows = [("user", None), ("assistant", members[0]["id"]), ("user", None)] + ([("user", None)] if extra_user else [])
    await _chain(conv["id"], [(role, f"Line {index}", speaker) for index, (role, speaker) in enumerate(rows)])


async def test_group_context_mode_defaults_to_private_and_rejects_unknown_values(client):
    conv, _ = await _two_card_group(client)
    assert conv["group_context_mode"] == "private"

    await client.put_checked(
        f"/api/conversations/{conv['id']}",
        json={"title": "Renamed", "group_context_mode": "everyone_sees_everything"},
        expected_status=422,
    )
    # A rejected payload must not half-apply: the title edit rode the same call.
    assert next(item for item in await client.get_json("/api/conversations") if item["id"] == conv["id"])["title"] == "Campfire"

    for mode in ("shared", "swap", "private"):
        response = await client.put_json(f"/api/conversations/{conv['id']}", json={"group_context_mode": mode})
        assert response["group_context_mode"] == mode


async def test_solo_conversations_are_unaffected_by_the_column(client, llm_mock):
    card_id = await _card(client, "Solo", description="SOLO PRIVATE")
    conv = await client.post_json("/api/conversations", json={"character_card_id": card_id})
    assert conv["group_context_mode"] == "private"
    llm_mock.enqueue_writer("A reply.")
    await _send(client, conv["id"], "Hi")
    system = _systems(llm_mock, "writer")[0]
    assert "## Cast" not in system and "## Character: Solo" in system


async def test_context_mode_rides_checkpoint_and_compression_forks(client):
    conv, members = await _two_card_group(client, context_mode="shared")
    await _three_lines(conv, members)
    assert (await client.post_json(f"/api/conversations/{conv['id']}/checkpoint", json={}))["group_context_mode"] == "shared"

    response = await client.post_json(f"/api/conversations/{conv['id']}/compress", json={"summary": "So far.", "keep_count": 2})
    forked = await client.get_json("/api/conversations")
    assert next(c for c in forked if c["id"] == response["new_conversation_id"])["group_context_mode"] == "shared"


async def test_shared_dossier_gives_every_speaker_one_prefix_and_never_repeats_identity(client, llm_mock):
    conv, _ = await _two_card_group(client, context_mode="shared")
    await _run_two_speaker_exchange(client, llm_mock, conv)

    systems = _systems(llm_mock, "writer")
    assert len(systems) == 2
    # Best prefix sharing: both speakers read the identical cast dossier body.
    assert systems[0] == systems[1]
    for system in systems:
        assert system.count("## Character dossier: Aria") == 1
        assert system.count("## Character dossier: Kael") == 1
        assert "ARIA PRIVATE" in system and "KAEL PRIVATE" in system

    # The identity fields are in the shared body, so the tail must not re-bill them; the speaker-only guard stays.
    aria_tail = json.dumps(_writers(llm_mock)[0]["messages"][-1])
    assert "ARIA PRIVATE" not in aria_tail and "ARIA EXAMPLE" not in aria_tail
    assert "Write the next reply as Aria only" in aria_tail


async def test_private_perspective_keeps_the_cast_prefix_stable_and_cards_speaker_local(client, llm_mock):
    conv, _ = await _two_card_group(client)
    await _run_two_speaker_exchange(client, llm_mock, conv)

    systems = _systems(llm_mock, "writer")
    assert systems[0] == systems[1]
    assert "ARIA PRIVATE" not in systems[0] and "KAEL PRIVATE" not in systems[0]
    writers = _writers(llm_mock)
    assert "ARIA PRIVATE" in json.dumps(writers[0]["messages"][-1])
    assert "ARIA PRIVATE" not in json.dumps(writers[1]["messages"][-1])


def _assert_cards_speaker_only(lane: list[str]) -> None:
    assert len(lane) == 2
    assert "ARIA PRIVATE" in lane[0] and "KAEL PRIVATE" not in lane[0]
    assert "KAEL PRIVATE" in lane[1] and "ARIA PRIVATE" not in lane[1]


@pytest.mark.kv_divergence_expected
async def test_classic_card_swap_uses_a_neutral_director_base_and_one_prefix_per_speaker(client, llm_mock):
    """Swap's per-speaker prefix is a deliberate cache divergence; the Director must still never see a member's card."""
    conv, _ = await _two_card_group(client, context_mode="swap")
    await _run_two_speaker_exchange(client, llm_mock, conv)

    director = _systems(llm_mock, "director")[0]
    assert "### Aria" in director and "### Kael" in director
    assert "ARIA PRIVATE" not in director and "KAEL PRIVATE" not in director

    systems = _systems(llm_mock, "writer")
    _assert_cards_speaker_only(systems)
    # The `index == 0` shortcut would have handed speaker 1 the neutral base.
    assert systems[0] != director and systems[0] != systems[1]
    # The public cast is speaker-independent, so it sits inside the region shared with the neutral base.
    shared_head = commonprefix([*systems, director])
    assert "### Aria" in shared_head and "### Kael" in shared_head, shared_head
    assert "ARIA PRIVATE" not in shared_head and "KAEL PRIVATE" not in shared_head


_GUARDED = {
    "enable_agent": True,
    "enabled_tools": {"direct_scene": True, "editor_apply_patch": True},
    "length_guard_enabled": True,
    "length_guard_max_words": 5,
}


@pytest.mark.parametrize("mode", ["private", "shared", "swap"])
async def test_the_editor_replays_the_exact_writer_input_in_every_mode(client, llm_mock, mode):
    """The Editor must extend the Writer's stack, never rebuild its own view of the cast."""
    await client.put("/api/settings", json=_GUARDED)
    conv, _ = await _two_card_group(client, context_mode=mode)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Look around"]))
    llm_mock.enqueue_writer("word " * 60)
    llm_mock.enqueue_editor(None)
    await _send(client, conv["id"], "Go on")

    writer = _writers(llm_mock)[0]
    editor = next((call for call in llm_mock.captured if call["pass"] == "editor"), None)
    assert editor is not None, "expected the editor to run"
    assert editor["messages"][: len(writer["messages"])] == writer["messages"]


async def _dual_model_exchange(client, llm_mock, mode: str) -> None:
    """Put director/editor on their own endpoint, writer on the active one, and run a two-speaker exchange."""
    ep = await client.post_json("/api/endpoints", json={"url": "http://agent.local", "api_key": "k"})
    await client.put_checked("/api/settings", json={"agent_same_as_writer": False, "agent_endpoint_id": ep["id"], **_GUARDED})
    conv, _ = await _two_card_group(client, context_mode=mode)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail", "kael — Explain the ward"]))
    for _ in range(2):
        llm_mock.enqueue_writer("word " * 60)
        llm_mock.enqueue_editor(None)
    await _send(client, conv["id"], "What happened?")


@pytest.mark.parametrize("mode", ["private", "shared"])
async def test_both_model_lanes_agree_on_the_cast_when_the_prefix_is_shared(client, llm_mock, mode):
    await _dual_model_exchange(client, llm_mock, mode)
    writers, editors = _systems(llm_mock, "writer"), _systems(llm_mock, "editor")
    assert len(writers) == 2 and len(editors) == 2
    assert len(set(writers)) == 1, "writer prefix diverged across speakers"
    assert len(set(editors)) == 1, "agent prefix diverged across speakers"
    # Different system prompts per lane are expected; the *cast body* is not.
    body = "## Character dossier: Aria" if mode == "shared" else "### Aria"
    assert body in writers[0] and body in editors[0]


@pytest.mark.kv_divergence_expected
async def test_classic_card_swap_swaps_the_card_on_the_agent_lane_too(client, llm_mock):
    """An Editor auditing Aria must not be reading Kael's card."""
    await _dual_model_exchange(client, llm_mock, "swap")
    _assert_cards_speaker_only(_systems(llm_mock, "writer"))
    _assert_cards_speaker_only(_systems(llm_mock, "editor"))
    assert "ARIA PRIVATE" not in _systems(llm_mock, "director")[0]


@pytest.mark.kv_divergence_expected
async def test_classic_card_swap_still_tells_every_speaker_the_public_cast(client, llm_mock):
    """Swap hides cards, not members: the curated profile rides every prefix, with the active card appended after it."""
    conv, members = await _two_card_group(client, context_mode="swap")
    # One member curated per scene, one falling back to its card-level profile.
    await client.put_checked(
        f"/api/characters/{members[1]['character_card_id']}/public-profile",
        json={"appearance": "Robed and hooded.", "role": "Keeper of the ward."},
    )
    await _put_members(
        client, conv, [{**members[0], "public_profile_override": "Role: the scout who found the trail."}, members[1]]
    )
    await _run_two_speaker_exchange(client, llm_mock, conv)

    systems = _systems(llm_mock, "writer")
    for system in [*systems, _systems(llm_mock, "director")[0]]:
        assert "### Aria\nRole: the scout who found the trail." in system
        assert "### Kael\nAppearance: Robed and hooded.\nRole: Keeper of the ward." in system
    _assert_cards_speaker_only(systems)


@pytest.mark.parametrize("mode", ["private", "shared"])
async def test_the_post_turn_steps_ride_the_exchange_base_rather_than_rebuilding_one(client, llm_mock, mode):
    """Dynamic Worlds and the direction-note step extend the speaker's frozen base rather than rebuilding a prefix."""
    world = await client.post_json("/api/worlds", json={"name": "Gorge", "is_global": True})
    await client.post(f"/api/worlds/{world['id']}/entries", json={"name": "Bridge", "content": "It groans.", "keywords": []})
    await client.put(f"/api/worlds/{world['id']}/dynamic", json={"enabled": True})
    await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})

    conv, _ = await _two_card_group(client, context_mode=mode)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Look around"]))
    llm_mock.enqueue_writer("The bridge gives way.")
    llm_mock.enqueue_world_change(
        [{"type": "function", "function": {"name": "propose_world_changes", "arguments": {"operations": []}}}]
    )
    await _send(client, conv["id"], "Go on")

    proposal = [c for c in llm_mock.captured if c["pass"] not in ("director", "writer")]
    assert proposal, "expected the Dynamic Worlds step to run"
    writer = _writers(llm_mock)[0]
    assert all(call["messages"][0] == writer["messages"][0] for call in proposal)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("private", ["cast_public", "largest_speaker_tail"]),
        ("shared", ["cast_dossiers", "largest_speaker_tail"]),
        ("swap", ["cast_public", "largest_active_card", "largest_speaker_tail"]),
    ],
)
async def test_context_size_breakdown_follows_the_context_mode(client, mode, expected):
    big = ("ARIA " * 40).strip()
    conv, _ = await _two_card_group(
        client, context_mode=mode, aria_extra={"description": big}, kael_extra={"description": "KAEL"}
    )
    breakdown = (await client.get_json(f"/api/conversations/{conv['id']}/context-size"))["breakdown"]
    assert [key for key in expected if key in breakdown] == expected
    # Exactly one shared-body key per mode -- a stale one would double-count.
    assert {"cast_public", "cast_dossiers"} & set(breakdown) == {expected[0]}
    # The biggest card is billed once wherever the mode puts it, never summed.
    billed = "largest_speaker_tail" if mode == "private" else ("largest_active_card" if mode == "swap" else "cast_dossiers")
    assert breakdown[billed]["chars"] >= len(big)
    assert breakdown["largest_speaker_tail"]["chars"] < len(big) or mode == "private"


@pytest.mark.parametrize("mode", ["private", "shared", "swap"])
async def test_compression_prompts_stay_on_the_public_cast_projection(client, llm_mock, mode):
    """Compression is scene-wide narration: no dossiers, and no arbitrary swapped-in card."""
    conv, members = await _two_card_group(client, context_mode=mode)
    await _chain(conv["id"], [row[:3] for row in _five_lines(members)])
    llm_mock.enqueue_writer("A summary.")
    await client.post_checked(f"/api/conversations/{conv['id']}/summarize", json={"keep_count": 2})
    system = str(llm_mock.captured[-1]["messages"][0]["content"])
    assert "### Aria" in system and "### Kael" in system
    assert "ARIA PRIVATE" not in system and "KAEL PRIVATE" not in system
    assert "## Character dossier" not in system


# -- The scene-local sheet override ------------------------------------------
# `public_profile_override` is what the rest of the cast sees; `card_sheet_override` is what the member reads about *itself*,
# scene-locally, without writing the card.


async def _put_members(client, conv, members: list[dict]):
    return await client.put_json(f"/api/conversations/{conv['id']}/members", json={"members": members})


def _member_spec(member: dict, **overrides) -> dict:
    keys = ("id", "character_card_id", "display_name", "public_profile_override", "card_sheet_override", "member_kind")
    return {**{key: member.get(key) for key in keys}, "muted": bool(member["muted"]), **overrides}


async def _override_sheet(client, conv, members, sheet: str):
    return await _put_members(client, conv, [_member_spec(members[0], card_sheet_override=sheet), _member_spec(members[1])])


async def test_an_empty_sheet_override_blanks_the_sheet_rather_than_restoring_the_card(client, llm_mock):
    """`""` is a deliberate blanking and `null` is absence; the server's resolution rule keeps them apart."""
    conv, members = await _two_card_group(client)
    assert (await _override_sheet(client, conv, members, ""))[0]["card_sheet_override"] == ""

    await _run_two_speaker_exchange(client, llm_mock, conv)
    writers = _writers(llm_mock)
    assert "ARIA PRIVATE" not in json.dumps(writers[0]["messages"][-1])
    # The other member is untouched: blanking is per-member, not per-scene.
    assert "KAEL PRIVATE" in json.dumps(writers[1]["messages"][-1])


async def test_the_sheet_override_rides_checkpoint_and_compression_forks(client):
    """Asserted on the *copied* member ids: `create_group_conversation` re-mints them."""
    conv, members = await _two_card_group(client)
    await _override_sheet(client, conv, members, "ARIA CURRENT SHEET")
    await _three_lines(conv, members)

    checkpoint = await client.post_json(f"/api/conversations/{conv['id']}/checkpoint", json={})
    compressed = await client.post_json(
        f"/api/conversations/{conv['id']}/compress", json={"summary": "So far.", "keep_count": 2}
    )
    for cid in (checkpoint["id"], compressed["new_conversation_id"]):
        forked = await _members(client, cid, "?include_inactive=true")
        assert not {member["id"] for member in forked} & {member["id"] for member in members}
        assert [member["card_sheet_override"] for member in forked] == ["ARIA CURRENT SHEET", None]


async def test_compression_never_re_asserts_a_members_sheet_into_the_summary(client, llm_mock):
    """The public-cast projection carries no sheet at all -- neither the card's nor the override's."""
    conv, members = await _two_card_group(client)
    await _override_sheet(client, conv, members, "ARIA SHEET OVERRIDE")
    await _three_lines(conv, members, extra_user=True)

    llm_mock.enqueue_writer("A summary.")
    await client.post_checked(f"/api/conversations/{conv['id']}/summarize", json={"keep_count": 2})
    system = str(llm_mock.captured[-1]["messages"][0]["content"])
    assert "ARIA SHEET OVERRIDE" not in system and "ARIA PRIVATE" not in system
    assert "### Aria" in system and "### Kael" in system


# -- The post-exchange sheet-update pass -----------------------------------------
# One call per member the exchange touched, staged pending, never applied. Routed through the mock's `workflow` queue for the
# reason `_profile_call` states: the schema is deliberately absent from `prompting.tool_catalog.TOOLS`.


def _sheet_call(**arguments) -> dict:
    """The forced ``update_character_sheet`` response."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "update_character_sheet", "arguments": arguments}}],
    }


async def _sheet_group(client, **kwargs) -> tuple[dict, list[dict]]:
    conv, members = await _two_card_group(client, **kwargs)
    updated = await client.put_json(f"/api/conversations/{conv['id']}", json={"group_sheet_updates": True})
    assert updated["group_sheet_updates"] == 1
    return updated, members


def _sheet_calls(llm_mock) -> list[str]:
    """Every sheet-update call's user message, in order."""
    texts = [str(call["messages"][-1]["content"]) for call in llm_mock.captured if call["pass"] == "workflow"]
    return [text for text in texts if "reference sheet" in text]


async def _proposals(client, conv, status: str | None = None) -> list[dict]:
    """The scene's proposals; no status means the route's own default review set."""
    return await client.get_json(
        f"/api/conversations/{conv['id']}/sheet-proposals", params={"status": status} if status else {}
    )


async def _staged_exchange(client, llm_mock, conv, *sheets: dict) -> None:
    """Run a two-speaker exchange whose sheet calls answer *sheets* (unchanged for the rest)."""
    for sheet in [*sheets, {}, {}][:2]:
        llm_mock.enqueue_workflow(_sheet_call(changed=True, **sheet) if sheet else _sheet_call(changed=False))
    await _run_two_speaker_exchange(client, llm_mock, conv)


_SHORN = {"sheet": "ARIA, shorn and coatless.", "summary": "Cut her hair"}


async def _proposal_action(client, conv, proposal: dict, action: str):
    return await client.post(f"/api/conversations/{conv['id']}/sheet-proposals/{proposal['id']}/{action}")


async def _aria_sheet(client, conv) -> str | None:
    return (await _members(client, conv["id"]))[0]["card_sheet_override"]


async def test_the_sheet_pass_is_off_until_the_scene_opts_in(client, llm_mock):
    conv, _ = await _two_card_group(client)
    assert conv["group_sheet_updates"] == 0
    await _run_two_speaker_exchange(client, llm_mock, conv)
    assert _sheet_calls(llm_mock) == []
    assert await _proposals(client, conv) == []


async def test_an_opted_in_exchange_stages_one_proposal_per_member_that_spoke(client, llm_mock):
    conv, members = await _sheet_group(client)
    await _staged_exchange(client, llm_mock, conv, _SHORN)
    assert len(_sheet_calls(llm_mock)) == 2
    pending = await _proposals(client, conv)
    assert [(item["member_id"], item["proposed_sheet"], item["summary"]) for item in pending] == [
        (members[0]["id"], "ARIA, shorn and coatless.", "Cut her hair")
    ]
    # Staged only: the member's sheet is untouched until the user applies.
    assert await _aria_sheet(client, conv) is None


async def test_a_silent_member_is_never_asked_about(client, llm_mock):
    conv, _ = await _sheet_group(client)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail"]))
    llm_mock.enqueue_writer("I found tracks.")
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _send(client, conv["id"], "What happened?")
    calls = _sheet_calls(llm_mock)
    assert len(calls) == 1 and "Aria" in calls[0] and "Character: Kael" not in calls[0]


async def test_each_sheet_call_carries_only_its_own_members_sheet(client, llm_mock):
    """Another member's *prose* is shared evidence; their *sheet* is not."""
    conv, _ = await _sheet_group(client)
    await _staged_exchange(client, llm_mock, conv)
    aria_call, kael_call = _sheet_calls(llm_mock)
    assert "ARIA PRIVATE" in aria_call and "KAEL PRIVATE" not in aria_call
    assert "KAEL PRIVATE" in kael_call and "ARIA PRIVATE" not in kael_call
    for call in (aria_call, kael_call):
        assert "I found tracks." in call and "The ward is broken." in call


async def test_the_pass_runs_once_per_exchange_not_once_per_speaker(client, llm_mock):
    conv, _ = await _sheet_group(client)
    for _ in range(4):
        llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _run_two_speaker_exchange(client, llm_mock, conv)
    assert len(_sheet_calls(llm_mock)) == 2


async def test_applying_a_proposal_changes_the_tail_and_leaves_the_cached_body_alone(client, llm_mock):
    """An applied update must cost no prefix rebuild, or keeping a scene current costs the KV cache."""
    conv, _ = await _sheet_group(client)
    await _staged_exchange(client, llm_mock, conv, _SHORN)
    before = _systems(llm_mock, "writer")

    applied = await _proposal_action(client, conv, (await _proposals(client, conv))[0], "apply")
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    assert await _aria_sheet(client, conv) == "ARIA, shorn and coatless."

    llm_mock.captured.clear()
    await _staged_exchange(client, llm_mock, conv)
    assert _systems(llm_mock, "writer") == before
    tail = json.dumps(_writers(llm_mock)[0]["messages"][-1])
    assert "shorn and coatless" in tail and "ARIA PRIVATE" not in tail


async def test_a_proposal_whose_sheet_moved_underneath_it_goes_stale_instead_of_clobbering(client, llm_mock):
    conv, members = await _sheet_group(client)
    await _staged_exchange(client, llm_mock, conv, _SHORN)
    pending = await _proposals(client, conv)

    await _override_sheet(client, conv, members, "ARIA, hand-edited.")
    assert (await _proposal_action(client, conv, pending[0], "apply")).status_code == 409
    # A refused proposal stays in the default listing, so it can say why.
    assert [(item["id"], item["status"]) for item in await _proposals(client, conv)] == [(pending[0]["id"], "stale")]
    assert await _aria_sheet(client, conv) == "ARIA, hand-edited."


async def test_rejecting_writes_nothing_and_retires_the_row(client, llm_mock):
    conv, _ = await _sheet_group(client)
    await _staged_exchange(client, llm_mock, conv, _SHORN)
    pending = await _proposals(client, conv)

    response = await _proposal_action(client, conv, pending[0], "reject")
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert await _proposals(client, conv) == []
    assert await _aria_sheet(client, conv) is None
    # A decided proposal is decided; a second apply cannot resurrect it.
    assert (await _proposal_action(client, conv, pending[0], "apply")).status_code == 409


async def test_a_second_exchange_replaces_the_pending_proposal_instead_of_stacking_beside_it(client, llm_mock):
    """Two pending proposals for one member are rivals; the later rewrites the earlier in place, built on its text."""
    conv, members = await _sheet_group(client)
    await _staged_exchange(client, llm_mock, conv, {"sheet": "ARIA, shorn.", "summary": "Cut her hair"})
    first = await _proposals(client, conv)
    assert len(first) == 1

    await _staged_exchange(client, llm_mock, conv, {"sheet": "ARIA, shorn and coatless.", "summary": "Coat burned"})
    pending = await _proposals(client, conv)
    assert len(pending) == 1, "one member, one undecided proposal"
    assert pending[0]["id"] == first[0]["id"], "the row is rewritten, not replaced beside"
    assert pending[0]["proposed_sheet"] == "ARIA, shorn and coatless."
    # The second call reasoned from the first proposal, not from the stored sheet.
    assert "ARIA, shorn." in _sheet_calls(llm_mock)[2]
    # `base_sheet` still names what an apply must match -- the *stored* sheet.
    assert pending[0]["base_sheet"] == "ARIA PRIVATE"
    assert (await _proposal_action(client, conv, pending[0], "apply")).status_code == 200
    assert await _aria_sheet(client, conv) == "ARIA, shorn and coatless."
    assert members[0]["id"] == pending[0]["member_id"]


async def test_a_hand_edit_stops_the_carry_forward(client, llm_mock):
    """A proposal whose base no longer matches the stored sheet is not carried forward."""
    conv, members = await _sheet_group(client)
    await _staged_exchange(client, llm_mock, conv, {"sheet": "ARIA, shorn.", "summary": "Cut her hair"})
    await _override_sheet(client, conv, members, "ARIA, hand-edited.")
    await _staged_exchange(client, llm_mock, conv)
    third = _sheet_calls(llm_mock)[2]
    assert "ARIA, hand-edited." in third and "ARIA, shorn." not in third


async def test_removing_a_member_retires_its_undecided_proposals(client, llm_mock):
    """A proposal left pending on a tombstoned member would sit in the review count with no row to dismiss it from."""
    conv, members = await _sheet_group(client)
    await _staged_exchange(client, llm_mock, conv, {"sheet": "ARIA, shorn.", "summary": "Cut her hair"})
    staged = (await _proposals(client, conv))[0]

    await _put_members(client, conv, [_member_spec(members[1])])
    assert await _proposals(client, conv) == []
    assert (await _proposals(client, conv, "all"))[0]["status"] == "rejected"
    assert (await _proposal_action(client, conv, staged, "apply")).status_code == 409


async def test_a_chip_click_reads_the_round_rather_than_its_own_request(client, llm_mock):
    """Under Manual one round is several requests; the sheet evidence is the round, not the request."""
    conv, members = await _sheet_group(client)
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Notice the trail"]))
    llm_mock.enqueue_writer("I found tracks.")
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await _send(client, conv["id"], "What happened?")

    llm_mock.enqueue_writer("The ward is broken.")
    llm_mock.enqueue_workflow(_sheet_call(changed=False))
    await client.post_checked(f"/api/conversations/{conv['id']}/speak", json={"speaker_member_id": members[1]["id"]})

    calls = _sheet_calls(llm_mock)
    assert len(calls) == 2
    assert "Character: Kael" in calls[1]
    for evidence in ("What happened?", "I found tracks.", "The ward is broken."):
        assert evidence in calls[1]
    # Only this request's speaker is proposed *about*.
    assert "Character: Aria" not in calls[1]


async def test_a_failed_sheet_call_never_costs_the_user_their_reply(client, llm_mock):
    conv, _ = await _sheet_group(client)
    llm_mock.enqueue_workflow(_sheet_call(changed=True, sheet="Has a {{char}} macro in it."))
    llm_mock.enqueue_workflow(_sheet_call(changed=True))  # reports a change, returns no sheet
    await _run_two_speaker_exchange(client, llm_mock, conv)

    assert await _proposals(client, conv) == []
    rows = await get_messages(conv["id"])
    assert [row["content"] for row in rows if row["role"] == "assistant"] == ["I found tracks.", "The ward is broken."]


async def test_group_activation_enables_cast_worlds_and_preserves_floating_worlds(client):
    names = ["Aria lore", "Kael lore", "Other character lore", "Global lore", "Retired global lore"]
    aria_world, kael_world, other_world, floating_on, floating_off = [
        await client.post_json("/api/worlds", json={"name": name, **({"is_global": True} if "Global" in name else {})})
        for name in names
    ]
    aria = await _card(client, "Aria", world_id=aria_world["id"])
    kael = await _card(client, "Kael", world_id=kael_world["id"])
    await _card(client, "Other", world_id=other_world["id"])
    for world in (aria_world, kael_world, floating_off):
        await client.put(f"/api/worlds/{world['id']}", json={"is_global": False})
    conv, _ = await _scene(client, [aria, kael])
    before = {world["id"]: world for world in await client.get_json("/api/worlds")}

    response = await client.get_json(f"/api/conversations/{conv['id']}/worlds")
    assert set(response["world_ids"]) == {aria_world["id"], kael_world["id"], floating_on["id"]}
    after = {world["id"]: world for world in await client.get_json("/api/worlds")}
    assert after == before, "Reading scene Worlds never changes defaults"


async def _three_message_exchange(client) -> tuple[dict, list[dict], list[int]]:
    conv, members = await _group(client, "Aria", "Kael")
    rows = [("user", "Question", None), ("assistant", "First", members[0]["id"]), ("assistant", "Second", members[1]["id"])]
    return conv, members, await _chain(conv["id"], [(*row, "original") for row in rows])


async def test_group_regenerate_and_magic_rewrite_keep_target_speaker_and_parent(client, db, llm_mock):
    conv, members, [_, first_id, target_id] = await _three_message_exchange(client)

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=[]))
    llm_mock.enqueue_writer("Kael: Replacement")
    await client.post_checked(f"/api/conversations/{conv['id']}/messages/{target_id}/regenerate", json={})
    replacement = await _latest_child(db, first_id)
    assert replacement["id"] != target_id
    assert replacement["speaker_member_id"] == members[1]["id"]
    assert replacement["content"] == "Replacement"

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=[]))
    llm_mock.enqueue_writer("**Kael:** Rewritten")
    await client.post_checked(
        f"/api/conversations/{conv['id']}/messages/{target_id}/magic_rewrite", json={"direction": "Make it quieter"}
    )
    rewritten = await _latest_child(db, first_id)
    assert rewritten["id"] not in (target_id, replacement["id"])
    assert rewritten["speaker_member_id"] == members[1]["id"]
    assert rewritten["content"] == "Rewritten"


async def test_pinned_speaker_still_gets_the_directors_cue(client, db, llm_mock):
    """A pin decides *who* speaks; the Director's cue for that very member still decides *what*."""
    conv, members, [_, first_id, target_id] = await _three_message_exchange(client)
    plan = ["aria — deflect the accusation calmly", "kael — explode at her perfect act"]
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=plan))
    llm_mock.enqueue_writer("Kael: Replacement")
    response = await client.post_checked(f"/api/conversations/{conv['id']}/messages/{target_id}/regenerate", json={})

    # The cast is the pin's, not the plan's: one reply, still Kael's.
    writers = _writers(llm_mock)
    assert len(writers) == 1
    assert (await _latest_child(db, first_id))["speaker_member_id"] == members[1]["id"]
    tail = writers[0]["messages"][-1]["content"]
    assert "## Your cue\nexplode at her perfect act" in tail
    assert "deflect the accusation calmly" not in tail
    # And it reaches the client, so the rail can show the reply's own cue.
    assert [(row["name"], row["cue"]) for row in _plan(response)["plan"]] == [("Kael", "explode at her perfect act")]


async def test_group_delete_preview_counts_invisible_sibling_replies(client):
    conv, [member] = await _group(client, "Aria")
    user_id, _ = await add_message(conv["id"], "user", "Question", 0)
    first_id, _ = await add_message(conv["id"], "assistant", "Visible", 1, parent_id=user_id, speaker_member_id=member["id"])
    sibling_id, _ = await add_message(
        conv["id"], "assistant", "Hidden sibling", 1, parent_id=user_id, speaker_member_id=member["id"]
    )
    await add_message(conv["id"], "assistant", "Hidden descendant", 2, parent_id=sibling_id, speaker_member_id=member["id"])
    await set_active_leaf(conv["id"], first_id)

    response = await client.get_json(f"/api/conversations/{conv['id']}/messages/{first_id}/delete-preview")
    assert response == {"message_count": 3, "assistant_count": 3}


async def test_group_fork_edit_runs_a_fresh_exchange_from_the_new_user_sibling(client, db, llm_mock):
    conv, [member] = await _group(client, "Aria")
    user_id, _ = await _chain(
        conv["id"], [("user", "Old question", None, "old"), ("assistant", "Old reply", member["id"], "old")]
    )
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Answer the edit"]))
    llm_mock.enqueue_writer("Fresh reply")
    await client.post_checked(f"/api/conversations/{conv['id']}/messages/{user_id}/fork-edit", json={"content": "New question"})
    new_user = await db.one(
        "SELECT * FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1", (conv["id"],)
    )
    new_reply = await db.one("SELECT * FROM messages WHERE parent_id = ?", (new_user["id"],))
    assert new_user["id"] != user_id and new_user["exchange_id"] != "old"
    assert new_reply["speaker_member_id"] == member["id"]
    assert new_reply["exchange_id"] == new_user["exchange_id"]


async def _enqueue_per_fragment_director(llm_mock, **arguments) -> None:
    """Queue one director response per step of the per-fragment loop (each fragment, then the plan, then moods)."""
    for _ in range(len([f for f in await get_interactive_fragments() if f.get("enabled", True)]) + 2):
        llm_mock.enqueue_director(_direct_scene(**arguments))


async def test_per_fragment_director_still_plans_a_group_exchange(client, llm_mock):
    """The speaking plan is one of the per-fragment fields, so it has to survive that loop."""
    await update_settings({"director_individual_fragments": 1})
    conv, members = await _two_card_group(client)
    await _enqueue_per_fragment_director(llm_mock, speaking_plan=["kael — Answer first"])
    llm_mock.enqueue_writer("The ward is broken.")

    response = await _send(client, conv["id"], "What happened?")
    assert not [data for name, data in _sse_events(response.text) if name == "error"]
    assert [item["member_id"] for item in _plan(response)["plan"]] == [members[1]["id"]]


async def test_an_intentional_rest_survives_both_director_shapes(client, llm_mock):
    """`[]` is the Director saying nobody answers; the per-fragment loop must not drop it as an empty value."""
    for individual in (0, 1):
        await update_settings({"director_individual_fragments": individual})
        conv, _ = await _two_card_group(client)
        if individual:
            await _enqueue_per_fragment_director(llm_mock, speaking_plan=[])
        else:
            llm_mock.enqueue_director(_direct_scene(speaking_plan=[]))

        response = await _send(client, conv["id"], "Nobody move.")
        assert _plan(response)["plan"] == [], f"director_individual_fragments={individual} overrode the rest"
        assert [m["role"] for m in await get_messages(conv["id"])] == ["user"]


async def test_a_missing_plan_falls_back_rather_than_resting(client, llm_mock):
    """A Director that never filled the field at all still gets the configured strategy."""
    await update_settings({"director_individual_fragments": 1})
    conv, members = await _two_card_group(client)
    await _enqueue_per_fragment_director(llm_mock, moods=[])
    llm_mock.enqueue_writer("I found tracks.")
    response = await _send(client, conv["id"], "What happened?")
    assert [item["member_id"] for item in _plan(response)["plan"]] == [members[0]["id"]]


async def test_group_steering_excludes_the_reply_it_replaces_from_the_audit(client, llm_mock):
    """The steered paths hand the editor a baseline window without the reply being replaced.

    Driven through the structural-repetition scanner: an identical draft is a finding when its twin is in the window and silence
    when it is not, so the editor firing at all is the observable.
    """
    # The editor only scans with the Agent on, its patch tool enabled and a phrase bank present.
    await update_settings({"enable_agent": 1, "enabled_tools": {"direct_scene": True, "editor_apply_patch": True}})
    await add_phrase_group(["a sharp intake of breath"])
    twin = "She crossed the yard, counted the lamps, and stopped at the gate."

    async def _steer(*, older: str, replaced: str) -> list[dict]:
        conv, members = await _two_card_group(client)
        aria = members[0]["id"]
        *_, target = await _chain(
            conv["id"],
            [("user", "Then?", None, "b0"), ("assistant", older, aria, "b0"), ("user", "And after?", None, "b1")]
            + [("assistant", replaced, aria, "b1")],
        )
        llm_mock.captured.clear()
        llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Try again"]))
        llm_mock.enqueue_writer(twin)
        await client.post_checked(f"/api/conversations/{conv['id']}/messages/{target}/super_regenerate", json={})
        return [call for call in llm_mock.captured if call["pass"] == "editor"]

    # Positive control: an older reply on the branch stays in the window, so an identical draft is caught.
    assert await _steer(older=twin, replaced="Kael shrugged and said nothing at all."), (
        "the structural scanner never fired, so the exclusion below is untested"
    )
    assert not await _steer(older="Kael shrugged and said nothing at all.", replaced=twin), (
        "the reply being replaced was audited against its own replacement"
    )


# -- Scene-profile drafting --------------------------------------------------
# The generator behind Manage cast's Draft / Redraft buttons. One LLM call per member, never batched.


def _profile_call(**arguments) -> dict:
    """The forced ``draft_public_profile`` response, routed to the mock's ``workflow`` queue."""
    return {"tool_calls": [{"type": "function", "function": {"name": "draft_public_profile", "arguments": arguments}}]}


def _drafting_message(llm_mock) -> str:
    return str(llm_mock.captured[-1]["messages"][-1]["content"])


async def _draft(client, llm_mock, cid: str, appearance="Tall.", role="Scout.", **body):
    llm_mock.enqueue_workflow(_profile_call(appearance=appearance, role=role))
    return await client.post(f"/api/conversations/{cid}/members/scene-profile/generate", json=body)


async def test_scene_profile_draft_renders_the_two_liner(client, llm_mock):
    """The same shape `_public_profile()` renders from a card; nothing is persisted until Save cast."""
    conv, members = await _two_card_group(client)
    response = await _draft(
        client,
        llm_mock,
        conv["id"],
        "Tall, in road-worn green.",
        "Scout of the watch.",
        character_card_id=members[0]["character_card_id"],
        display_name="Aria",
    )
    assert response.status_code == 200
    assert response.json() == {"profile": "Appearance: Tall, in road-worn green.\nRole: Scout of the watch."}
    assert (await _members(client, conv["id"]))[0]["public_profile_override"] is None


async def test_scene_profile_draft_sends_only_the_target_card_and_other_names(client, llm_mock):
    """The executable form of the no-batching decision: Kael's card must never enter Aria's drafting context."""
    conv, members = await _two_card_group(client, kael_extra={"description": "KAEL SECRET", "personality": "KAEL INNER"})
    response = await _draft(
        client,
        llm_mock,
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
    """The premise comes from the server, not the modal."""
    conv, members = await _two_card_group(client)
    await client.put(f"/api/conversations/{conv['id']}", json={"character_scenario": "A cold night on the wall."})
    await _draft(client, llm_mock, conv["id"], character_card_id=members[0]["character_card_id"])
    assert "A cold night on the wall." in _drafting_message(llm_mock)


async def test_scene_profile_draft_seeds_the_card_level_profile_as_the_default(client, llm_mock):
    """A card-level profile is what this scene's override replaces, so the model adjusts it."""
    conv, members = await _two_card_group(client)
    card_id = members[0]["character_card_id"]
    await client.put_checked(
        f"/api/characters/{card_id}/public-profile", json={"appearance": "Green cloak, longbow.", "role": "Ranger."}
    )
    await _draft(client, llm_mock, conv["id"], character_card_id=card_id)
    sent = _drafting_message(llm_mock)
    assert "Appearance: Green cloak, longbow.\nRole: Ranger." in sent
    assert "the default this scene's profile replaces" in sent
    assert "Adjust that default" in sent


async def test_scene_profile_draft_for_a_cardless_member_is_a_sentence_not_a_422(client, llm_mock):
    conv, _ = await _two_card_group(client)
    response = await client.post_checked(
        f"/api/conversations/{conv['id']}/members/scene-profile/generate",
        json={"display_name": "Narrator"},
        expected_status=400,
    )
    # A prose `detail`, not FastAPI's validation list, and it names what went wrong.
    detail = response.json()["detail"]
    assert isinstance(detail, str) and "narrator" in detail.lower()


async def test_scene_profile_draft_works_for_a_row_not_yet_on_the_roster(client, llm_mock):
    """Manage cast is client-side until Save, so drafting must not require a roster row."""
    conv, members = await _two_card_group(client)
    newcomer = await _card(client, "Mira", description="MIRA PRIVATE")
    response = await _draft(
        client,
        llm_mock,
        conv["id"],
        "Small, quick.",
        "Thief.",
        character_card_id=newcomer,
        display_name="Mira",
        cast_names=["Aria", "Kael"],
    )
    assert response.status_code == 200
    assert response.json()["profile"] == "Appearance: Small, quick.\nRole: Thief."
    assert "MIRA PRIVATE" in _drafting_message(llm_mock)
    assert await _members(client, conv["id"]) == members  # the roster is untouched by a draft


async def test_omitted_cast_names_fall_back_to_the_stored_roster_without_the_target(client, llm_mock):
    conv, members = await _two_card_group(client)
    await _draft(client, llm_mock, conv["id"], character_card_id=members[0]["character_card_id"])
    sent = _drafting_message(llm_mock)
    assert "Kael" in sent
    # Aria is the target; she is named as the subject, never as an other member.
    assert "names only)" in sent
    assert "Aria" not in sent.split("names only):")[1].split('"""')[1]


async def test_a_large_cast_is_bounded_and_says_how_many_it_left_out(client, llm_mock):
    """A prompt-size guard, not a roster limit, and the prompt says the list is partial."""
    conv, members = await _two_card_group(client)
    await _draft(
        client,
        llm_mock,
        conv["id"],
        character_card_id=members[0]["character_card_id"],
        cast_names=[f"Extra{i}" for i in range(17)],
    )
    sent = _drafting_message(llm_mock)
    assert "Extra15" in sent and "Extra16" not in sent
    assert "Other cast members omitted from this draft: 1" in sent


async def test_a_checkpoint_carries_the_scenes_sheet_update_opt_in(client, llm_mock):
    """Checkpoint, Compress History and "New scene in this group" all fork; the opt-in must ride every fork."""
    conv, _ = await _sheet_group(client)
    response = await client.post_json(f"/api/conversations/{conv['id']}/checkpoint", json={"title": "Checkpoint"})
    assert response["group_sheet_updates"] == 1
    assert (await client.post_json(f"/api/conversations/{conv['id']}/group-conversation"))["group_sheet_updates"] == 1


async def test_a_reply_the_next_speaker_reads_is_the_one_the_row_holds(client, llm_mock):
    """One inline-macro roll per reply, shared by the DB row and the next speaker's in-memory history."""
    conv, _ = await _group(client, "Aria", "Kael", title="Campfire")
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — Roll", "kael — Answer"]))
    llm_mock.enqueue_writer("The die shows {{roll::1d20}}.")
    llm_mock.enqueue_writer("Kael nods.")

    response = await _send(client, conv["id"], "Roll for it.")
    messages = await get_messages(conv["id"])
    stored = next(m["content"] for m in messages if m["speaker_member_id"] and "die shows" in m["content"])
    assert "{{roll" not in stored, "the persist boundary did not resolve the macro"

    wire = json.dumps(_writers(llm_mock)[1]["messages"])
    assert "{{roll" not in wire, "the next speaker read an unresolved macro"
    assert stored in wire, "the next speaker read a different roll than the row holds"
    # And the SSE the browser painted the bubble from agrees with both.
    done = [data for name, data in _sse_events(response.text) if name == "speaker_done"]
    assert done[0]["content"] == stored  # type: ignore[index]
