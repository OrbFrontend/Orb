"""Turn-level coverage for branch-aware Director fragment cooldowns."""

import pytest

import backend.database as dbmod
from backend.pipeline import handle_regenerate, handle_turn


async def _drain(agen) -> list[dict]:
    return [event async for event in agen]


def _direct_scene(arguments: dict) -> list[dict]:
    return [{"type": "function", "function": {"name": "direct_scene", "arguments": arguments}}]


async def _turn(llm_mock, cid: str, message: str, arguments: dict, reply: str = "ok") -> list[dict]:
    llm_mock.enqueue_director(_direct_scene(arguments))
    llm_mock.enqueue_writer(reply)
    return await _drain(handle_turn(cid, message))


async def _setup(client, cid: str) -> None:
    await dbmod.create_conversation(cid, "cooldown", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})


_STORMY = {"id": "stormy", "label": "Stormy", "description": "Use during conflict.", "cooldown_turns": 3}
_TRUST = {
    "id": "trust",
    "label": "Trust",
    "description": "Current trust level.",
    "field_type": "state",
    "state_mode": "value",
    "state_update": "before_writer",
    "state_inject": "writer",
    "injection_label": "Trust",
    "cooldown_turns": 2,
}


def _director_data(events: list[dict]) -> dict:
    return next(event["data"] for event in events if event.get("event") == "director_done")


async def _last_assistant(cid: str) -> dict:
    return next(message for message in reversed(await dbmod.get_messages(cid)) if message["role"] == "assistant")


async def test_mood_is_blocked_for_exact_cooldown_without_negative_prompt(client, db, llm_mock):
    cid = "conv-fragment-cooldown-mood"
    await _setup(client, cid)
    await client.post(
        "/api/fragments",
        json={**_STORMY, "prompt_text": "Write with stormy intensity.", "negative_prompt": "Return to a calmer register."},
    )

    first = _director_data(await _turn(llm_mock, cid, "one", {"moods": ["stormy"]}))
    assert first["active_moods"] == ["stormy"]
    assert first["fragment_cooldowns"] == {"stormy": 3}

    for index, remaining in enumerate((2, 1, 0), start=2):
        data = _director_data(await _turn(llm_mock, cid, str(index), {"moods": ["stormy"]}))
        assert data["active_moods"] == []
        assert data["fragment_cooldowns"].get("stormy", 0) == remaining
        if index == 2:
            assert "Return to a calmer register." not in data["injection_block"]

    available = _director_data(await _turn(llm_mock, cid, "five", {"moods": ["stormy"]}))
    assert available["active_moods"] == ["stormy"]
    assert available["fragment_cooldowns"] == {"stormy": 3}


@pytest.mark.parametrize(
    "disabled",
    [
        pytest.param({"enabled_tools": {"direct_scene": False}}, id="direction"),
        pytest.param({"enable_agent": False}, id="agent", marks=pytest.mark.kv_divergence_expected),
    ],
)
async def test_disabled_direction_clears_turn_moods_and_keeps_saved_state(client, db, llm_mock, disabled):
    cid = "conv-disabled-direction-moods"
    await _setup(client, cid)
    await client.post_checked("/api/fragments", json={**_STORMY, "prompt_text": "Stormy prose."})
    await client.post_checked(
        "/api/fragments",
        json={
            "id": "steady",
            "label": "Steady",
            "description": "Use for a calm scene.",
            "prompt_text": "Steady prose.",
            "negative_prompt": "Drop the steady mood.",
        },
    )
    await client.post_checked("/api/interactive-fragments", json=_TRUST)
    first = _director_data(await _turn(llm_mock, cid, "one", {"moods": ["steady", "stormy"], "trust": "guarded"}))
    assert first["active_moods"] == ["steady", "stormy"]
    original = await _last_assistant(cid)

    await client.put_checked("/api/settings", json=disabled)
    # A carried mood must not fire a newly configured cooldown while Direction is off.
    await client.put_checked("/api/fragments/steady", json={"cooldown_turns": 2})
    estimate = await client.get_json(f"/api/conversations/{cid}/context-size")
    capture_start = len(llm_mock.captured)
    llm_mock.enqueue_writer("No direction this turn.")
    events = await _drain(handle_turn(cid, "two"))
    assert not any(event["event"] in ("director_start", "error") for event in events)
    data = _director_data(events)
    assert data["active_moods"] == []
    assert data["fragment_cooldowns"] == {"stormy": 2, "trust": 1}
    assert data["tool_calls"] == []
    assert "Trust: guarded" in data["injection_block"]
    assert estimate["breakdown"]["director_injection"]["chars"] == len(data["injection_block"])
    writer = next(call for call in llm_mock.captured[capture_start:] if call["pass"] == "writer")
    prompt = "\n".join(str(message["content"]) for message in writer["messages"])
    assert "Trust: guarded" in prompt
    assert all(text not in prompt for text in ("Steady prose.", "Stormy prose.", "Drop the steady mood."))

    reply = await _last_assistant(cid)
    saved = await client.get_json(f"/api/conversations/{cid}/messages/{reply['id']}/director-log")
    assert saved["mood_data_available"] is True
    assert saved["active_moods"] == []
    assert saved["injection_block"] == data["injection_block"]
    assert (await dbmod.get_director_state(cid))["active_moods"] == []
    previous = await client.get_json(f"/api/conversations/{cid}/messages/{original['id']}/director-log")
    assert previous["active_moods"] == first["active_moods"]

    llm_mock.enqueue_writer("Regenerated without direction.")
    regenerated = _director_data(await _drain(handle_regenerate(cid, reply["id"])))
    assert regenerated["active_moods"] == []
    assert regenerated["fragment_cooldowns"] == data["fragment_cooldowns"]

    await client.put_checked("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})
    resumed = _director_data(await _turn(llm_mock, cid, "three", {"moods": []}))
    assert resumed["active_moods"] == []
    assert "Drop the steady mood." not in resumed["injection_block"]
    assert "Trust: guarded" in resumed["injection_block"]
    assert resumed["fragment_cooldowns"] == {"stormy": 1}


async def test_resting_state_value_is_kept_and_injected_without_restarting_cooldown(client, db, llm_mock):
    cid = "conv-fragment-cooldown-state"
    await _setup(client, cid)
    await client.post("/api/interactive-fragments", json=_TRUST)

    await _turn(llm_mock, cid, "one", {"moods": [], "trust": "guarded"})
    resting = _director_data(await _turn(llm_mock, cid, "two", {"moods": [], "trust": "model changed it"}))
    # A resting fragment's value is rejected this turn, kept, and still injected.
    assert "trust" not in resting["extra_fields"]
    assert "Trust: guarded" in resting["injection_block"]
    assert resting["fragment_cooldowns"] == {"trust": 1}
    last = await _last_assistant(cid)
    assert await dbmod.get_state_events_for_message(last["id"]) == []
    path = await dbmod.get_messages(cid)
    view = await dbmod.fold_path_state(cid, [m["id"] for m in path])
    assert [entry.text for entry in view.active("trust")] == ["guarded"]


async def test_state_value_cooldown_starts_only_when_the_value_changes(client, db, llm_mock):
    cid = "conv-fragment-cooldown-state-echo"
    await _setup(client, cid)
    await client.post("/api/interactive-fragments", json=_TRUST)

    assert _director_data(await _turn(llm_mock, cid, "one", {"moods": [], "trust": "guarded"}))["fragment_cooldowns"] == {
        "trust": 2
    }
    await _turn(llm_mock, cid, "two", {"moods": []})
    await _turn(llm_mock, cid, "three", {"moods": []})
    # Echoing the saved value changes nothing, so the fragment stays available.
    assert _director_data(await _turn(llm_mock, cid, "four", {"moods": [], "trust": "guarded"}))["fragment_cooldowns"] == {}
    assert await dbmod.get_state_events_for_message((await _last_assistant(cid))["id"]) == []
    changed = _director_data(await _turn(llm_mock, cid, "five", {"moods": [], "trust": "warming"}))
    assert changed["fragment_cooldowns"] == {"trust": 2}


async def test_regenerate_rewinds_cooldown_and_checkpoint_copies_snapshot(client, db, llm_mock):
    cid = "conv-fragment-cooldown-branch"
    await _setup(client, cid)
    await client.post("/api/fragments", json={**_STORMY, "prompt_text": "Stormy prose."})

    await _turn(llm_mock, cid, "one", {"moods": ["stormy"]})
    await _turn(llm_mock, cid, "two", {"moods": ["stormy"]})
    target = await _last_assistant(cid)
    assert target["fragment_cooldowns"] == {"stormy": 2}

    for reply in ("redo one", "redo two"):
        llm_mock.enqueue_director(_direct_scene({"moods": ["stormy"]}))
        llm_mock.enqueue_writer(reply)
        await _drain(handle_regenerate(cid, target["id"]))
        assert (await _last_assistant(cid))["fragment_cooldowns"] == {"stormy": 2}

    response = await client.post_json(f"/api/conversations/{cid}/checkpoint", json={"title": "copy"})
    assert (await _last_assistant(response["id"]))["fragment_cooldowns"] == {"stormy": 2}


async def test_inspector_distinguishes_missing_mood_history_from_empty_selection(client, db, llm_mock):
    cid = "conv-mood-history"
    await _setup(client, cid)
    await _turn(llm_mock, cid, "hello", {"moods": []})
    reply = await _last_assistant(cid)
    url = f"/api/conversations/{cid}/messages/{reply['id']}/director-log"
    recorded = await client.get_json(url)
    assert recorded["mood_data_available"] is True
    assert recorded["active_moods"] == []

    await db.execute("DELETE FROM conversation_logs WHERE conversation_id = ?", (cid,))
    await db.commit()
    missing = await client.get_json(url)
    assert missing["mood_data_available"] is False
    assert missing["active_moods"] == []


async def test_regeneration_drops_a_mood_disabled_since_the_baseline(client, db, llm_mock):
    cid = "conv-disabled-mood-regenerate"
    await _setup(client, cid)
    await client.post_checked(
        "/api/fragments", json={**_STORMY, "cooldown_turns": 0, "prompt_text": "Stormy prose.", "negative_prompt": "Calm down."}
    )
    await _turn(llm_mock, cid, "one", {"moods": ["stormy"]})
    await _turn(llm_mock, cid, "two", {"moods": ["stormy"]})
    reply = await _last_assistant(cid)

    await client.put_checked("/api/fragments/stormy", json={"enabled": False})
    capture_start = len(llm_mock.captured)
    llm_mock.enqueue_director(_direct_scene({"moods": []}))
    llm_mock.enqueue_writer("again")
    data = _director_data(await _drain(handle_regenerate(cid, reply["id"])))

    prompts = "\n".join(str(message["content"]) for call in llm_mock.captured[capture_start:] for message in call["messages"])
    assert "stormy" not in prompts.lower()
    assert data["active_moods"] == []
