"""Turn-level integration tests for the Editor Feedback step.

Covers the post-writer, user-facing note (now run inside the editor pass): that a
``feedback`` SSE event fires with values when an enabled ``field_type='feedback'``
fragment is present and ``feedback_enabled`` is on, that the values persist in
``conversation_logs.feedback``, that the step is skipped when its gate is off,
and that no ``give_feedback`` content leaks into the writer's prompt.
"""

from __future__ import annotations

import json

import backend.database as dbmod
from backend.orchestrator import handle_turn


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


# The feedback field_type is a single string, so give_feedback returns one.
_FEEDBACK_NOTE = "Ask her name, or quietly leave the room."
_GIVE_FEEDBACK_CALL = [
    {
        "type": "function",
        "function": {
            "name": "give_feedback",
            "arguments": {"suggested_actions": _FEEDBACK_NOTE},
        },
    }
]


async def _enable_feedback(client) -> None:
    # Agent off keeps the pipeline to writer + feedback (no director/editor to
    # enqueue). Feedback is gated by feedback_enabled AND an enabled feedback-type
    # fragment.
    await client.put("/api/settings", json={"enable_agent": False, "feedback_enabled": True})
    await client.put("/api/interactive-fragments/suggested_actions", json={"enabled": True})


async def test_feedback_event_fires_and_persists(client, db, llm_mock):
    cid = "conv-feedback"
    await dbmod.create_conversation(cid, "feedback", "Bot", "a scenario")
    await _enable_feedback(client)

    llm_mock.enqueue_writer("She looks up at you, startled.")
    llm_mock.enqueue_feedback(_GIVE_FEEDBACK_CALL)

    events = await _drain(handle_turn(cid, "hello"))

    feedback_events = [e for e in events if e.get("event") == "feedback"]
    assert len(feedback_events) == 1, f"expected one feedback event, got {len(feedback_events)}"
    assert feedback_events[0]["data"]["values"] == {"suggested_actions": _FEEDBACK_NOTE}

    # Persisted on the turn's conversation_logs row, JSON-decoded by the reader.
    logs = await dbmod.get_conversation_logs(cid)
    assert len(logs) == 1
    assert logs[0]["feedback"] == {"suggested_actions": _FEEDBACK_NOTE}


async def test_feedback_skipped_when_setting_off(client, db, llm_mock):
    cid = "conv-feedback-off"
    await dbmod.create_conversation(cid, "feedback", "Bot", "a scenario")
    # Enable the feedback fragment but leave feedback_enabled off.
    await client.put("/api/settings", json={"enable_agent": False, "feedback_enabled": False})
    await client.put("/api/interactive-fragments/suggested_actions", json={"enabled": True})

    llm_mock.enqueue_writer("She looks up at you, startled.")

    events = await _drain(handle_turn(cid, "hello"))

    assert not [e for e in events if e.get("event") == "feedback"]
    # No feedback pass ran: the LLM mock saw writer only.
    assert not any(p == "feedback" for p, _ in llm_mock.calls)
    logs = await dbmod.get_conversation_logs(cid)
    assert logs[0]["feedback"] == {}


async def test_feedback_does_not_leak_into_writer_prompt(client, db, llm_mock):
    cid = "conv-feedback-leak"
    await dbmod.create_conversation(cid, "feedback", "Bot", "a scenario")
    await _enable_feedback(client)

    llm_mock.enqueue_writer("She looks up at you, startled.")
    llm_mock.enqueue_feedback(_GIVE_FEEDBACK_CALL)

    await _drain(handle_turn(cid, "hello"))

    writer_calls = [c for c in llm_mock.captured if c["pass"] == "writer"]
    assert len(writer_calls) == 1
    wc = writer_calls[0]

    # give_feedback now rides the shared per-turn tools blob (Invariant 3), so in
    # single-model mode the writer ships it too — byte-identical with the feedback
    # call's blob. It is the *schema* that rides the blob, not the prompt.
    tool_names = [t["function"]["name"] for t in (wc["tools"] or [])]
    assert "give_feedback" in tool_names

    # The schema rides the tools blob, not the writer's messages: neither the tool
    # name nor the feedback fragment's id reach the writer prompt.
    writer_text = json.dumps(wc["messages"])
    assert "give_feedback" not in writer_text
    assert "suggested_actions" not in writer_text

    # The feedback step carries the full shared blob (not a swapped give_feedback-
    # only blob) and forces tool_choice to give_feedback.
    feedback_calls = [c for c in llm_mock.captured if c["pass"] == "feedback"]
    assert len(feedback_calls) == 1
    fb_tool_names = [t["function"]["name"] for t in (feedback_calls[0]["tools"] or [])]
    assert "give_feedback" in fb_tool_names
    assert feedback_calls[0]["tool_choice"] == {"type": "function", "function": {"name": "give_feedback"}}

    # The feedback step reuses the writer's cached base verbatim: same tools blob.
    assert feedback_calls[0]["tools"] == wc["tools"]
