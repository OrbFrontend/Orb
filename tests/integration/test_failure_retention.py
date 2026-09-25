"""A failure after billed calls keeps what those calls produced.

A Writer failure still ends the turn with the terminal ``error`` event, and the
save keeps the Director's moods and cooldowns on the reply row and its Scene
Guidance, tool calls and reasoning in the Inspector's log. A failing Editor
call is only a ``warning``: the turn completes with the edits that finished,
and the Editor's remaining sub-steps still run.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import backend.database as dbmod
from backend.pipeline import handle_turn
from backend.pipeline.persistence import _consume_pipeline
from backend.pipeline.state import TurnState

REPLY = "Her voice was barely a whisper."


async def _drain(agen) -> list[dict]:
    return [event async for event in agen]


def _error(events: list[dict]) -> dict:
    return next(event["data"] for event in events if event["event"] == "error")


def _direct_scene(**arguments) -> list[dict]:
    return [{"type": "function", "function": {"name": "direct_scene", "arguments": arguments}}]


def _search_replace(search: str, replace: str, *, call_id: str) -> list[dict]:
    return [
        {
            "id": call_id,
            "type": "function",
            "function": {
                "name": "editor_search_replace",
                "arguments": {"patches": [{"search": search, "replace": replace}]},
            },
        }
    ]


async def _post_processing_fragment(client, fid: str, sort_order: int) -> None:
    response = await client.post(
        "/api/interactive-fragments",
        json={
            "id": fid,
            "label": fid,
            "injection_label": fid.replace("_", " ").title(),
            "description": "Tighten the prose.",
            "field_type": "post_processing",
            "required": False,
            "enabled": True,
            "sort_order": sort_order,
        },
    )
    assert response.status_code == 200, response.text


async def _directed_turn_setup(client, llm_mock, cid: str) -> None:
    """A conversation whose Director picks the seeded ``tense`` mood (3-turn cooldown)."""
    await dbmod.create_conversation(cid, "retention", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})
    assert (await client.put("/api/fragments/tense", json={"cooldown_turns": 3})).status_code == 200
    llm_mock.enqueue_reasoning("director", "The scene wants dread.")
    llm_mock.enqueue_director(_direct_scene(moods=["tense"]))


async def _assistant(cid: str) -> dict:
    replies = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"]
    assert len(replies) == 1
    return replies[0]


async def _director_log(client, cid: str, message_id: int) -> dict:
    response = await client.get(f"/api/conversations/{cid}/messages/{message_id}/director-log")
    assert response.status_code == 200
    return response.json()


async def _assert_director_record_kept(client, cid: str, reply: dict) -> dict:
    assert reply["fragment_cooldowns"] == {"tense": 3}
    assert (await dbmod.get_director_state(cid))["active_moods"] == ["tense"]
    log = await _director_log(client, cid, reply["id"])
    assert log["active_moods"] == ["tense"]
    assert log["injection_block"].startswith("**Scene Guidance**")
    assert [call["name"] for call in log["tool_calls"]][:1] == ["direct_scene"]
    assert log["reasoning_director"] == "The scene wants dread."
    return log


def _warnings(events: list[dict]) -> list[dict]:
    return [event["data"] for event in events if event["event"] == "warning"]


def _feedback_call(text: str) -> list[dict]:
    return [{"id": "fb1", "type": "function", "function": {"name": "give_feedback", "arguments": {"suggested_actions": text}}}]


async def _enable_feedback(client) -> None:
    await client.put("/api/interactive-fragments/suggested_actions", json={"enabled": True})


async def test_an_editor_timeout_warns_and_the_turn_completes_with_the_finished_edits(client, llm_mock):
    cid = "conv-retention-editor"
    await _directed_turn_setup(client, llm_mock, cid)
    await _enable_feedback(client)
    await _post_processing_fragment(client, "first_fix", 1)
    await _post_processing_fragment(client, "second_fix", 2)
    await _post_processing_fragment(client, "third_fix", 3)
    llm_mock.enqueue_writer(REPLY)
    llm_mock.enqueue_post_processing(_search_replace("barely a whisper", "a low murmur", call_id="pp1"))
    llm_mock.fail("post_processing", httpx.ReadTimeout("timed out"), after=1)
    llm_mock.enqueue_post_processing(_search_replace("Her voice", "His voice", call_id="pp3"))
    llm_mock.enqueue_feedback(_feedback_call("Ask what she heard."))

    events = await _drain(handle_turn(cid, "hello"))

    assert not [event for event in events if event["event"] == "error"]
    assert events[-1]["event"] == "done"
    [warning] = _warnings(events)
    assert warning["headline"] == "Post-processing “second_fix” didn't finish."
    assert warning["stage"] == "editor pass"
    assert "timed out" in warning["sentence"]
    # The fragment after the failed one and the feedback step still ran.
    assert [name for name, _ in llm_mock.calls if name in ("post_processing", "feedback")] == [
        "post_processing",
        "post_processing",
        "post_processing",
        "feedback",
    ]
    edited = "His voice was a low murmur."
    reply = await _assistant(cid)
    assert reply["content"] == edited
    log = await _assert_director_record_kept(client, cid, reply)
    assert [call["name"] for call in log["tool_calls"]].count("editor_search_replace") == 2
    assert log["feedback"] == {"suggested_actions": "Ask what she heard."}


async def test_a_feedback_timeout_warns_and_keeps_the_reply(client, llm_mock):
    cid = "conv-retention-feedback"
    await _directed_turn_setup(client, llm_mock, cid)
    await _enable_feedback(client)
    llm_mock.enqueue_writer(REPLY)
    llm_mock.fail("feedback", httpx.ReadTimeout("timed out"))

    events = await _drain(handle_turn(cid, "hello"))

    assert not [event for event in events if event["event"] == "error"]
    assert [warning["headline"] for warning in _warnings(events)] == ["Feedback didn't finish."]
    reply = await _assistant(cid)
    assert reply["content"] == REPLY
    log = await _assert_director_record_kept(client, cid, reply)
    assert log["feedback"] == {}


async def test_a_writer_dropping_mid_stream_keeps_the_director_record(client, llm_mock):
    cid = "conv-retention-writer"
    await _directed_turn_setup(client, llm_mock, cid)
    llm_mock.enqueue_reasoning("writer", "Keep it short.")
    llm_mock.enqueue_writer("Her voice was")
    llm_mock.fail("writer", httpx.RemoteProtocolError("peer closed connection"), mid_stream=True)

    events = await _drain(handle_turn(cid, "hello"))

    assert _error(events)["stage"] == "writer pass"
    reply = await _assistant(cid)
    assert reply["content"] == "Her voice was"
    log = await _assert_director_record_kept(client, cid, reply)
    assert log["reasoning_writer"] == "Keep it short."


async def test_a_failure_before_any_reply_text_saves_nothing(client, llm_mock):
    cid = "conv-retention-empty"
    await _directed_turn_setup(client, llm_mock, cid)
    llm_mock.fail("writer", httpx.ReadTimeout("timed out"))

    events = await _drain(handle_turn(cid, "hello"))

    assert _error(events)["stage"] == "writer pass"
    assert [message["role"] for message in await dbmod.get_messages(cid)] == ["user"]
    assert await dbmod.get_conversation_logs(cid) == []
    # No reply node to anchor them to, so the Director's moods do not commit.
    assert (await dbmod.get_director_state(cid))["active_moods"] == []


async def test_cancelling_the_fallback_save_writes_the_reply_once(client, monkeypatch):
    cid = "conv-retention-cancel"
    await dbmod.create_conversation(cid, "retention", "Bot", "a scenario")
    user_id, _ = await dbmod.add_message(cid, "user", "hello", 0, advance_leaf=True)
    real_add_message = dbmod.add_message
    reached, release = asyncio.Event(), asyncio.Event()
    inserts: list[str] = []

    async def slow_add_message(*args, **kwargs):
        inserts.append(args[1])
        reached.set()
        await release.wait()
        return await real_add_message(*args, **kwargs)

    monkeypatch.setattr(dbmod, "add_message", slow_add_message)

    async def failing():
        yield {"event": "_turn_state", "data": TurnState(resp_text="Partial")}
        raise RuntimeError("connection lost")

    settings = await dbmod.get_settings()
    task = asyncio.create_task(_drain(_consume_pipeline(failing(), cid, settings, user_id, 1)))
    await reached.wait()
    task.cancel()
    release.set()
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await task

    assert inserts == ["assistant"]
    assert [message["content"] for message in await dbmod.get_messages(cid) if message["role"] == "assistant"] == ["Partial"]
