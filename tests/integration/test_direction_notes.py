"""Turn-level integration tests for the direction-note step.

A direction note is authored by an interactive fragment of ``field_type="direction_note"``:
the forced ``record_direction_note`` sub-call exposes one parameter per enabled such
fragment, and each filled parameter persists one note (keyed to the turn's assistant
message, carrying the fragment's id and label). Covers both placements, the suppressing
gates (off / global agent toggle / no enabled fragment / empty draft), branch-dependence,
the steered regenerates, and the read/write separation: recording (the ``direction_notes_record``
switch + per-fragment ``enabled`` and ``direction_note_timing``) is independent of injection
(``direction_notes_inject``).
"""

from __future__ import annotations

import backend.database as dbmod
from backend.pipeline import (
    handle_magic_rewrite,
    handle_regenerate,
    handle_turn,
)

_NOTE = "Alice now distrusts the user, after he lied about the key."
_HEADING = "Direction of travel"


async def _make_fragment(
    fid: str = "trajectory", injection_label: str = _HEADING, enabled: bool = True, timing: str = "post_turn"
) -> None:
    """Create one enabled ``field_type="direction_note"`` interactive fragment."""
    await dbmod.create_interactive_fragment(
        {
            "id": fid,
            "label": fid.title(),
            "description": f"Record the {injection_label}.",
            "field_type": "direction_note",
            "injection_label": injection_label,
            "enabled": enabled,
            "direction_note_timing": timing,
        }
    )


def _record_call(**fields: str) -> list[dict]:
    """A ``record_direction_note`` tool call filling one parameter per fragment id."""
    return [{"type": "function", "function": {"name": "record_direction_note", "arguments": dict(fields)}}]


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


async def _notes_on_active_path(cid: str) -> list[str]:
    path = await dbmod.get_messages(cid)
    rows = await dbmod.get_direction_notes_for_path(cid, [m["id"] for m in path])
    return [r["content"] for r in rows]


async def _last_assistant(cid: str):
    msgs = await dbmod.get_messages(cid)
    return [m for m in msgs if m["role"] == "assistant"][-1]


async def _injection_block(events: list[dict]) -> str:
    blocks = [e["data"]["injection_block"] for e in events if e.get("event") == "director_done"]
    return blocks[-1] if blocks else ""


async def test_post_turn_fires_and_persists(client, db, llm_mock):
    cid = "conv-dn-post"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment()
    await client.put("/api/settings", json={"enable_agent": True, "direction_notes_record": True})

    llm_mock.enqueue_writer("She nods slowly.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))

    events = await _drain(handle_turn(cid, "hello"))

    pevents = [e for e in events if e.get("event") == "direction_notes"]
    assert len(pevents) == 1
    assert [n["content"] for n in pevents[0]["data"]["notes"]] == [_NOTE]

    rows = await dbmod.get_direction_notes_for_message((await _last_assistant(cid))["id"])
    assert len(rows) == 1
    assert rows[0]["content"] == _NOTE
    assert rows[0]["interactive_fragment_id"] == "trajectory"
    assert rows[0]["interactive_fragment_label"] == _HEADING


async def test_pre_writer_runs_before_writer(client, db, llm_mock):
    cid = "conv-dn-pre"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment(timing="pre_writer")
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "direction_notes_record": True, "enabled_tools": {"direct_scene": True}},
    )

    llm_mock.enqueue_director([{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": []}}}])
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))
    llm_mock.enqueue_writer("He turns away without a word.")

    await _drain(handle_turn(cid, "hello"))

    order = [p for p, _ in llm_mock.calls]
    assert "direction_note" in order
    assert order.index("direction_note") < order.index("writer")
    assert await _notes_on_active_path(cid) == [_NOTE]


async def test_pre_writer_skipped_without_direct_scene(client, db, llm_mock):
    cid = "conv-dn-pre-skip"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment(timing="pre_writer")
    # pre_writer reflects on the director's scene direction, so without direct_scene
    # the sub-call must not run.
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "direction_notes_record": True, "enabled_tools": {"direct_scene": False}},
    )

    llm_mock.enqueue_writer("A reply.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))

    await _drain(handle_turn(cid, "hello"))

    assert not any(p == "direction_note" for p, _ in llm_mock.calls)
    assert await _notes_on_active_path(cid) == []


async def test_off_does_not_run(client, db, llm_mock):
    cid = "conv-dn-off"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment()
    await client.put("/api/settings", json={"enable_agent": True, "direction_notes_record": False})

    llm_mock.enqueue_writer("A reply.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))  # must stay unconsumed

    events = await _drain(handle_turn(cid, "hello"))

    assert not [e for e in events if e.get("event") == "direction_notes"]
    assert not any(p == "direction_note" for p, _ in llm_mock.calls)
    assert await _notes_on_active_path(cid) == []


async def test_obeys_global_agent_toggle(client, db, llm_mock):
    cid = "conv-dn-agent-off"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment()
    await client.put("/api/settings", json={"enable_agent": False, "direction_notes_record": True})

    llm_mock.enqueue_writer("A reply.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))

    await _drain(handle_turn(cid, "hello"))

    assert not any(p == "direction_note" for p, _ in llm_mock.calls)
    assert await _notes_on_active_path(cid) == []


async def test_empty_draft_persists_nothing(client, db, llm_mock):
    cid = "conv-dn-empty"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment()
    await client.put("/api/settings", json={"enable_agent": True, "direction_notes_record": True})

    llm_mock.enqueue_writer("")  # reasoning-only turn: no assistant message persisted
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))

    await _drain(handle_turn(cid, "hello"))

    # post_turn is gated on a non-empty draft, so the sub-call never fires and no row lands.
    assert not any(p == "direction_note" for p, _ in llm_mock.calls)
    assert await _notes_on_active_path(cid) == []


async def test_notes_are_branch_dependent(client, db, llm_mock):
    cid = "conv-dn-branch"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment()
    await client.put("/api/settings", json={"enable_agent": True, "direction_notes_record": True})

    llm_mock.enqueue_writer("The door creaks open.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))
    await _drain(handle_turn(cid, "hello"))

    asst1 = await _last_assistant(cid)
    assert await _notes_on_active_path(cid) == [_NOTE]

    # Regenerate that reply: a new sibling whose path excludes asst1, so asst1's note is
    # not active. The sub-call records nothing this time (nothing enqueued).
    llm_mock.enqueue_writer("The door stays shut.")
    await _drain(handle_regenerate(cid, asst1["id"]))
    assert await _notes_on_active_path(cid) == []

    # Switching back to the original branch restores the note.
    await dbmod.switch_to_branch(cid, asst1["id"])
    assert await _notes_on_active_path(cid) == [_NOTE]


async def test_magic_rewrite_records_note_on_new_branch(client, db, llm_mock):
    cid = "conv-dn-magic"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment()
    await client.put("/api/settings", json={"enable_agent": True, "direction_notes_record": True})

    llm_mock.enqueue_writer("The hall is silent.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))
    await _drain(handle_turn(cid, "hello"))
    asst1 = await _last_assistant(cid)
    assert await _notes_on_active_path(cid) == [_NOTE]

    # Magic-rewrite runs the full pipeline as a new sibling whose path excludes asst1,
    # so the sub-call fires again and keys its note to the new reply, not the old one.
    note_b = "The user now carries the iron key openly."
    llm_mock.enqueue_writer("The hall echoes with footsteps.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=note_b))
    events = await _drain(handle_magic_rewrite(cid, asst1["id"], "make it louder"))

    emitted = [e["data"]["notes"] for e in events if e.get("event") == "direction_notes"]
    assert [[n["content"] for n in notes] for notes in emitted] == [[note_b]]

    asst2 = await _last_assistant(cid)
    assert asst2["id"] != asst1["id"]
    assert await _notes_on_active_path(cid) == [note_b]
    assert [r["content"] for r in await dbmod.get_direction_notes_for_message(asst2["id"])] == [note_b]


async def test_injection_is_independent_of_recording(client, db, llm_mock):
    cid = "conv-dn-rw"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment()
    # direct_scene on so a director_done injection block is produced each turn.
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "direction_notes_record": True,
            "direction_notes_inject": "both",
            "enabled_tools": {"direct_scene": True},
        },
    )

    # Turn 1 records a note.
    llm_mock.enqueue_writer("The lamp flickers.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))
    await _drain(handle_turn(cid, "hello"))

    # Turn 2, inject on: the stored note appears in the injection block.
    llm_mock.enqueue_writer("Shadows lengthen.")
    events2 = await _drain(handle_turn(cid, "again"))
    block2 = await _injection_block(events2)
    assert _HEADING in block2 and _NOTE in block2

    # Turn 3, inject off: the note is withheld from the prompt, yet recording still runs.
    await client.put("/api/settings", json={"direction_notes_inject": "off"})
    llm_mock.enqueue_writer("A new arrival.")
    llm_mock.enqueue_direction_note(_record_call(trajectory="A stranger entered."))
    events3 = await _drain(handle_turn(cid, "more"))
    block3 = await _injection_block(events3)
    assert _NOTE not in block3
    assert "A stranger entered." in await _notes_on_active_path(cid)


async def test_disabling_fragment_stops_new_notes_keeps_old(client, db, llm_mock):
    cid = "conv-dn-disable"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment("alpha", "Alpha heading")
    await _make_fragment("beta", "Beta heading")
    await client.put("/api/settings", json={"enable_agent": True, "direction_notes_record": True})

    # Turn 1: both fragments record.
    llm_mock.enqueue_writer("Opening.")
    llm_mock.enqueue_direction_note(_record_call(alpha="a-note", beta="b-note"))
    await _drain(handle_turn(cid, "hello"))
    assert set(await _notes_on_active_path(cid)) == {"a-note", "b-note"}

    # Disable beta: it no longer contributes a tool parameter, so a returned beta value
    # is dropped -- but beta's already-recorded note is untouched (enable gates writing only).
    await client.put("/api/interactive-fragments/beta", json={"enabled": False})
    llm_mock.enqueue_writer("Continuing.")
    llm_mock.enqueue_direction_note(_record_call(alpha="a-note-2", beta="b-note-2"))
    await _drain(handle_turn(cid, "again"))

    notes = await _notes_on_active_path(cid)
    assert "a-note-2" in notes  # alpha still records
    assert "b-note-2" not in notes  # beta disabled -> its value is dropped
    assert "b-note" in notes  # beta's prior note survives


def _director_scene_prompt(llm_mock) -> str:
    """Text of the most recent direct_scene director call's user message."""
    for cap in reversed(llm_mock.captured):
        tc = cap.get("tool_choice")
        if cap["pass"] == "director" and isinstance(tc, dict) and tc.get("function", {}).get("name") == "direct_scene":
            content = cap["messages"][-1]["content"]
            if isinstance(content, str):
                return content
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


async def _record_then_next_turn(client, llm_mock, cid: str, recipient: str) -> list[dict]:
    """Record _NOTE on turn 1 (post_turn), then run a second turn so the note is loaded and
    injected; returns the second turn's events. direct_scene is on so the director runs."""
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment()
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "direction_notes_record": True,
            "enabled_tools": {"direct_scene": True},
            "direction_notes_inject": recipient,
        },
    )
    direct_scene = [{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": []}}}]
    llm_mock.enqueue_director(direct_scene)
    llm_mock.enqueue_writer("She steps inside.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))
    await _drain(handle_turn(cid, "hello"))

    llm_mock.enqueue_director(direct_scene)
    llm_mock.enqueue_writer("She looks around.")
    return await _drain(handle_turn(cid, "again"))


async def test_recipient_director_only(client, db, llm_mock):
    events = await _record_then_next_turn(client, llm_mock, "conv-dn-rec-dir", "director")
    prompt = _director_scene_prompt(llm_mock)
    assert _NOTE in prompt  # the director's direct_scene prompt sees the note...
    assert "turn 1" in prompt  # ...tagged with the turn it was recorded on
    assert _NOTE not in await _injection_block(events)  # ...but the writer's Scene Direction does not


async def test_recipient_writer_only(client, db, llm_mock):
    events = await _record_then_next_turn(client, llm_mock, "conv-dn-rec-wri", "writer")
    assert _NOTE not in _director_scene_prompt(llm_mock)  # the director does not see it
    block = await _injection_block(events)
    assert _NOTE in block and "turn 1" in block  # the writer does, tagged with the turn


async def test_recipient_both_reaches_director_and_writer(client, db, llm_mock):
    events = await _record_then_next_turn(client, llm_mock, "conv-dn-rec-both", "both")
    assert _NOTE in _director_scene_prompt(llm_mock)
    assert _NOTE in await _injection_block(events)


async def test_fragment_routes_list_edit_delete(client, db, llm_mock):
    cid = "conv-dn-routes"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment()
    await client.put("/api/settings", json={"enable_agent": True, "direction_notes_record": True})
    llm_mock.enqueue_writer("A reply.")
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))
    await _drain(handle_turn(cid, "hello"))

    listing = (await client.get(f"/api/conversations/{cid}/direction-notes")).json()
    assert len(listing) == 1
    assert listing[0]["content"] == _NOTE
    assert listing[0]["interactive_fragment_label"] == _HEADING
    assert "turn_index" in listing[0]
    fid = listing[0]["id"]

    edited = await client.put(f"/api/conversations/{cid}/direction-notes/{fid}", json={"content": "edited"})
    assert edited.status_code == 200
    assert edited.json()["content"] == "edited"

    deleted = await client.delete(f"/api/conversations/{cid}/direction-notes/{fid}")
    assert deleted.status_code == 200
    assert (await client.get(f"/api/conversations/{cid}/direction-notes")).json() == []

    assert (await client.delete(f"/api/conversations/{cid}/direction-notes/99999")).status_code == 404


async def test_get_for_path_empty_and_membership(client, db, llm_mock):
    cid = "conv-dn-recon"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    uid, _ = await dbmod.add_message(cid, "user", "hi", 0)
    on_path, _ = await dbmod.add_message(cid, "assistant", "yo", 1, parent_id=uid)
    off_path, _ = await dbmod.add_message(cid, "assistant", "sibling", 1, parent_id=uid)
    note = {"interactive_fragment_id": "f", "interactive_fragment_label": "F", "content": "on path"}
    await dbmod.create_direction_notes(cid, on_path, [note])
    await dbmod.create_direction_notes(cid, off_path, [{**note, "content": "off path"}])

    assert await dbmod.get_direction_notes_for_path(cid, []) == []
    rows = await dbmod.get_direction_notes_for_path(cid, [uid, on_path])
    assert [r["content"] for r in rows] == ["on path"]


async def test_get_for_path_orders_by_branch_not_row_id(client, db, llm_mock):
    cid = "conv-dn-order"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    early, _ = await dbmod.add_message(cid, "user", "early", 0)
    mid, _ = await dbmod.add_message(cid, "assistant", "reply", 1, parent_id=early)
    late, _ = await dbmod.add_message(cid, "user", "late", 2, parent_id=mid)

    # Record on the later message first, then the earlier one, so the earlier-turn note gets the
    # higher row id -- the exact shape a user authoring a note onto a past message produces.
    note = {"interactive_fragment_id": "human", "interactive_fragment_label": "N", "content": "late-turn note"}
    await dbmod.create_direction_notes(cid, late, [note])
    await dbmod.create_direction_notes(cid, early, [{**note, "content": "early-turn note"}])

    # Branch order, not row-id order: the early-message note comes first despite its newer row.
    # Ordering by id alone (the bug) would invert these.
    rows = await dbmod.get_direction_notes_for_path(cid, [early, mid, late])
    assert [r["content"] for r in rows] == ["early-turn note", "late-turn note"]
    assert [r["message_id"] for r in rows] == [early, late]


async def test_deleting_message_removes_its_notes(client, db, llm_mock):
    cid = "conv-dn-del"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    uid, _ = await dbmod.add_message(cid, "user", "hi", 0)
    aid, _ = await dbmod.add_message(cid, "assistant", "yo", 1, parent_id=uid)
    await dbmod.set_active_leaf(cid, aid)
    await dbmod.create_direction_notes(
        cid, aid, [{"interactive_fragment_id": "human", "interactive_fragment_label": "N", "content": "note on the reply"}]
    )
    assert [r["content"] for r in await dbmod.get_direction_notes_for_path(cid, [uid, aid])] == ["note on the reply"]

    # Removing the assistant message cascade-deletes its note (FK ON DELETE CASCADE, foreign_keys=ON)
    # and moves the active leaf to the parent, so the note leaves both the DB and the branch path.
    assert await dbmod.delete_message_with_descendants(cid, aid) is True
    assert await dbmod.get_direction_notes_for_message(aid) == []
    path_ids = [m["id"] for m in await dbmod.get_messages(cid)]
    assert await dbmod.get_direction_notes_for_path(cid, path_ids) == []


async def test_per_fragment_timing_runs_both_steps(client, db, llm_mock):
    cid = "conv-dn-timing"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment("early", "Early heading", timing="pre_writer")
    await _make_fragment("late", "Late heading", timing="post_turn")
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "direction_notes_record": True, "enabled_tools": {"direct_scene": True}},
    )

    llm_mock.enqueue_director([{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": []}}}])
    llm_mock.enqueue_direction_note(_record_call(early="recorded early"))
    llm_mock.enqueue_writer("A reply lands.")
    llm_mock.enqueue_direction_note(_record_call(late="recorded late"))

    events = await _drain(handle_turn(cid, "hello"))

    # A pre-writer step (early) and a post-turn step (late) both fire, on either side of the writer.
    order = [p for p, _ in llm_mock.calls]
    dn_positions = [i for i, p in enumerate(order) if p == "direction_note"]
    assert len(dn_positions) == 2
    assert dn_positions[0] < order.index("writer") < dn_positions[1]

    # Both notes persist, and the final event carries the running total across both steps.
    assert set(await _notes_on_active_path(cid)) == {"recorded early", "recorded late"}
    final = [e["data"]["notes"] for e in events if e.get("event") == "direction_notes"][-1]
    assert {n["content"] for n in final} == {"recorded early", "recorded late"}


async def test_user_note_route_creates_and_lists(client, db, llm_mock):
    cid = "conv-dn-user"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    # A user note is authored through the route, not the model's step: no fragment and no
    # recording needed. A plain turn just gives it an assistant message to anchor to.
    await client.put("/api/settings", json={"enable_agent": True})
    llm_mock.enqueue_writer("She waits.")
    await _drain(handle_turn(cid, "hello"))
    asst = await _last_assistant(cid)

    created = await client.post(
        f"/api/conversations/{cid}/direction-notes",
        json={"message_id": asst["id"], "label": "My label", "content": "The user owns the iron key."},
    )
    assert created.status_code == 200

    rows = await dbmod.get_direction_notes_for_message(asst["id"])
    assert len(rows) == 1
    assert rows[0]["interactive_fragment_id"] == "human"
    assert rows[0]["interactive_fragment_label"] == "My label"
    assert rows[0]["content"] == "The user owns the iron key."

    # It lands on the active path and the list route stamps it with the turn, like a model note.
    listing = (await client.get(f"/api/conversations/{cid}/direction-notes")).json()
    assert [(n["interactive_fragment_id"], n["content"]) for n in listing] == [("human", "The user owns the iron key.")]
    assert "turn_index" in listing[0]

    # A message from no conversation (or another) is rejected; empty content is rejected.
    assert (
        await client.post(
            f"/api/conversations/{cid}/direction-notes", json={"message_id": 999999, "label": "x", "content": "y"}
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/conversations/{cid}/direction-notes", json={"message_id": asst["id"], "label": "x", "content": "  "}
        )
    ).status_code == 400


async def test_user_note_anchors_to_user_message(client, db, llm_mock):
    cid = "conv-dn-user-msg"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    # The Notes button surfaces on user messages, so a note can anchor to the user's own turn
    # rather than the reply. The route already takes any on-conversation message id; this pins
    # that a user-message anchor persists, lists with that message's turn, and injects on the
    # next turn exactly as a reply-anchored note does. direction_notes_record stays off -- the
    # note is route-authored, not recorded by the model step.
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "enabled_tools": {"direct_scene": True}, "direction_notes_inject": "director"},
    )
    direct_scene = [{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": []}}}]
    llm_mock.enqueue_director(direct_scene)
    llm_mock.enqueue_writer("She waits.")
    await _drain(handle_turn(cid, "hello"))

    user_msg = [m for m in await dbmod.get_messages(cid) if m["role"] == "user"][-1]
    created = await client.post(
        f"/api/conversations/{cid}/direction-notes",
        json={"message_id": user_msg["id"], "label": "Mine", "content": _NOTE},
    )
    assert created.status_code == 200

    rows = await dbmod.get_direction_notes_for_message(user_msg["id"])
    assert [(r["interactive_fragment_id"], r["content"]) for r in rows] == [("human", _NOTE)]

    # The list route stamps the note with its anchor message's turn -- the user message's, here.
    listing = (await client.get(f"/api/conversations/{cid}/direction-notes")).json()
    assert listing[0]["message_id"] == user_msg["id"]
    assert listing[0]["turn_index"] == user_msg["turn_index"]

    # Next turn: the director's direct_scene prompt sees the note, proving a user-anchored note
    # injects like any other on-path note.
    llm_mock.enqueue_director(direct_scene)
    llm_mock.enqueue_writer("She looks around.")
    await _drain(handle_turn(cid, "again"))
    assert _NOTE in _director_scene_prompt(llm_mock)


async def test_per_fragment_records_one_call_per_fragment(client, db, llm_mock):
    cid = "conv-dn-perfrag"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment("alpha", "Alpha heading")
    await _make_fragment("beta", "Beta heading")
    # director_individual_fragments splits the post-turn group into one record call per
    # fragment. direct_scene off so only the notes step runs (no director per-fragment noise).
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "direction_notes_record": True,
            "director_individual_fragments": True,
            "enabled_tools": {"direct_scene": False},
        },
    )

    llm_mock.enqueue_writer("A reply lands.")
    # Each call runs against the union wire schema, so a reply may carry both parameters;
    # extraction keeps only the call's own fragment. Queue the same reply for both calls.
    both = _record_call(alpha="a-note", beta="b-note")
    llm_mock.enqueue_direction_note(both)
    llm_mock.enqueue_direction_note(both)

    await _drain(handle_turn(cid, "hello"))

    # One record sub-call per fragment, not a single combined call.
    assert [p for p, _ in llm_mock.calls].count("direction_note") == 2
    # Each call contributed only its own fragment's value, under that fragment's heading.
    rows = await dbmod.get_direction_notes_for_message((await _last_assistant(cid))["id"])
    assert {r["interactive_fragment_label"]: r["content"] for r in rows} == {
        "Alpha heading": "a-note",
        "Beta heading": "b-note",
    }


async def test_per_fragment_call_can_record_nothing(client, db, llm_mock):
    cid = "conv-dn-perfrag-decline"
    await dbmod.create_conversation(cid, "dn", "Bot", "a scenario")
    await _make_fragment("alpha", "Alpha heading")
    await _make_fragment("beta", "Beta heading")
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "direction_notes_record": True,
            "director_individual_fragments": True,
            "enabled_tools": {"direct_scene": False},
        },
    )

    llm_mock.enqueue_writer("A reply lands.")
    # Only one reply queued: the first per-fragment call records, the second dequeues the mock's
    # empty default (nothing worth keeping) and contributes no note, leaving the first intact.
    llm_mock.enqueue_direction_note(_record_call(alpha="a-note", beta="b-note"))

    await _drain(handle_turn(cid, "hello"))

    assert [p for p, _ in llm_mock.calls].count("direction_note") == 2
    assert len(await _notes_on_active_path(cid)) == 1


async def test_group_exchange_records_pre_writer_notes_once_on_its_first_reply(client, db, llm_mock):
    """A group's Director runs once for the whole exchange, so the step that reflects
    on its scene direction runs once too — beside it, not inside each speaker's
    pipeline. The notes anchor to the exchange's first reply and are not re-recorded
    by the speakers after it."""
    aria = (await client.post("/api/characters", json={"name": "Aria"})).json()["id"]
    kael = (await client.post("/api/characters", json={"name": "Kael"})).json()["id"]
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
    await _make_fragment(timing="pre_writer")
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "direction_notes_record": True, "enabled_tools": {"direct_scene": True}},
    )

    llm_mock.enqueue_director(
        [
            {
                "type": "function",
                "function": {
                    "name": "direct_scene",
                    "arguments": {"moods": [], "speaking_plan": ["aria — Notice", "kael — Answer"]},
                },
            }
        ]
    )
    llm_mock.enqueue_direction_note(_record_call(trajectory=_NOTE))
    llm_mock.enqueue_writer("Aria speaks.")
    llm_mock.enqueue_writer("Kael answers.")

    await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})

    order = [p for p, _ in llm_mock.calls]
    assert order.count("direction_note") == 1, "the exchange's Director step ran per speaker"
    assert order.index("direction_note") < order.index("writer")
    assert await _notes_on_active_path(conv["id"]) == [_NOTE]
    replies = [m for m in await dbmod.get_messages(conv["id"]) if m["role"] == "assistant"]
    assert len(await dbmod.get_direction_notes_for_message(replies[0]["id"])) == 1
    assert await dbmod.get_direction_notes_for_message(replies[1]["id"]) == []


async def test_group_exchange_keeps_the_two_placements_on_their_own_replies(client, db, llm_mock):
    """Both placements run once per exchange, and each lands where it was recorded:
    the before-writer note on the first reply, the end-of-turn note on the last.
    The exchange carries the first one to its reply and then stops carrying it, so the
    later speaker neither re-persists it nor absorbs it into its own recording."""
    aria = (await client.post("/api/characters", json={"name": "Aria"})).json()["id"]
    kael = (await client.post("/api/characters", json={"name": "Kael"})).json()["id"]
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
    await _make_fragment("early", "Early heading", timing="pre_writer")
    await _make_fragment("late", "Late heading", timing="post_turn")
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "direction_notes_record": True, "enabled_tools": {"direct_scene": True}},
    )

    llm_mock.enqueue_director(
        [
            {
                "type": "function",
                "function": {
                    "name": "direct_scene",
                    "arguments": {"moods": [], "speaking_plan": ["aria — Notice", "kael — Answer"]},
                },
            }
        ]
    )
    llm_mock.enqueue_direction_note(_record_call(early="before-note"))
    llm_mock.enqueue_writer("Aria speaks.")
    llm_mock.enqueue_writer("Kael answers.")
    llm_mock.enqueue_direction_note(_record_call(late="after-note"))

    await client.post(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})

    assert [p for p, _ in llm_mock.calls].count("direction_note") == 2
    replies = [m for m in await dbmod.get_messages(conv["id"]) if m["role"] == "assistant"]
    first = [r["content"] for r in await dbmod.get_direction_notes_for_message(replies[0]["id"])]
    last = [r["content"] for r in await dbmod.get_direction_notes_for_message(replies[1]["id"])]
    assert first == ["before-note"]
    assert last == ["after-note"]
    assert sorted(await _notes_on_active_path(conv["id"])) == ["after-note", "before-note"]
