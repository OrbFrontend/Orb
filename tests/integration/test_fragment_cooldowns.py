"""Turn-level coverage for branch-aware Director fragment cooldowns."""

from __future__ import annotations

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


def _director_data(events: list[dict]) -> dict:
    return next(event["data"] for event in events if event.get("event") == "director_done")


async def _last_assistant(cid: str) -> dict:
    return next(message for message in reversed(await dbmod.get_messages(cid)) if message["role"] == "assistant")


async def test_mood_is_blocked_for_exact_cooldown_without_negative_prompt(client, db, llm_mock):
    cid = "conv-fragment-cooldown-mood"
    await _setup(client, cid)
    await client.post(
        "/api/fragments",
        json={
            "id": "stormy",
            "label": "Stormy",
            "description": "Use during conflict.",
            "prompt_text": "Write with stormy intensity.",
            "negative_prompt": "Return to a calmer register.",
            "cooldown_turns": 3,
        },
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


async def test_resting_progressive_value_is_carried_without_restarting_cooldown(client, db, llm_mock):
    cid = "conv-fragment-cooldown-progressive"
    await _setup(client, cid)
    await client.post(
        "/api/interactive-fragments",
        json={
            "id": "trust",
            "label": "Trust",
            "description": "Current trust level.",
            "field_type": "progressive",
            "injection_label": "Trust",
            "cooldown_turns": 2,
        },
    )

    await _turn(llm_mock, cid, "one", {"moods": [], "trust": "guarded"})
    resting = _director_data(await _turn(llm_mock, cid, "two", {"moods": [], "trust": "model changed it"}))
    assert resting["extra_fields"]["trust"] == "guarded"
    assert "Trust (Current trust level.): guarded" in resting["injection_block"]
    assert resting["fragment_cooldowns"] == {"trust": 1}
    assert (await _last_assistant(cid))["progressive_fields"] == {"trust": "guarded"}


async def test_regenerate_rewinds_cooldown_and_checkpoint_copies_snapshot(client, db, llm_mock):
    cid = "conv-fragment-cooldown-branch"
    await _setup(client, cid)
    await client.post(
        "/api/fragments",
        json={
            "id": "stormy",
            "label": "Stormy",
            "description": "Use during conflict.",
            "prompt_text": "Stormy prose.",
            "cooldown_turns": 3,
        },
    )

    await _turn(llm_mock, cid, "one", {"moods": ["stormy"]})
    await _turn(llm_mock, cid, "two", {"moods": ["stormy"]})
    target = await _last_assistant(cid)
    assert target["fragment_cooldowns"] == {"stormy": 2}

    for reply in ("redo one", "redo two"):
        llm_mock.enqueue_director(_direct_scene({"moods": ["stormy"]}))
        llm_mock.enqueue_writer(reply)
        await _drain(handle_regenerate(cid, target["id"]))
        assert (await _last_assistant(cid))["fragment_cooldowns"] == {"stormy": 2}

    response = await client.post(f"/api/conversations/{cid}/checkpoint", json={"title": "copy"})
    assert response.status_code == 200
    copied = await _last_assistant(response.json()["id"])
    assert copied["fragment_cooldowns"] == {"stormy": 2}
