"""Conversation-turn coverage for post-processing interactive fragments."""

import asyncio
import importlib
import json
import sqlite3

import backend.database as dbmod
from backend.inference import DecisionClient, DecisionResponse
from backend.pipeline import handle_turn
from tests.integration.workflows._fixtures import (  # noqa: F401
    _restore_registry,
    make_workflow,
    register_for_test,
)


async def _drain(agen) -> list[dict]:
    return [event async for event in agen]


def _call(search: str, replace: str, *, call_id: str) -> list[dict]:
    return [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": "editor_find_replace", "arguments": {"patches": [{"find": search, "replace": replace}]}},
        }
    ]


async def _create_fragment(client, fid: str, instruction: str, sort_order: int, gate: str = "") -> None:
    await client.post_checked(
        "/api/interactive-fragments",
        json={
            "id": fid,
            "label": fid,
            "injection_label": fid.replace("_", " ").title(),
            "description": instruction,
            "field_type": "post_processing",
            "required": False,
            "enabled": True,
            "sort_order": sort_order,
            "post_processing_gate": gate,
        },
    )


async def test_ordered_fragments_edit_before_feedback_workflow_and_persistence(client, llm_mock):
    cid = "conv-post-processing"
    await dbmod.create_conversation(cid, "post", "Bot", "a scenario")
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "reasoning_enabled_passes": {"director": False, "writer": False, "editor": True}},
    )
    await client.put("/api/interactive-fragments/suggested_actions", json={"enabled": True})
    await _create_fragment(client, "second_edit", "Make the greeting warmer.", 9)
    await _create_fragment(client, "first_edit", "Make the greeting casual.", 8)

    seen_by_workflow: list[str] = []

    async def post_hook(ctx):
        seen_by_workflow.append(ctx.draft)
        return
        yield  # pragma: no cover

    llm_mock.enqueue_writer("Hello there.")
    llm_mock.enqueue_post_processing(_call("Hello", "Hey", call_id="p1"))
    llm_mock.enqueue_post_processing(_call("Hey there.", "Hey, friend.", call_id="p2"))
    llm_mock.enqueue_reasoning("post_processing", "first edit reasoning")
    llm_mock.enqueue_reasoning("post_processing", "second edit reasoning")
    llm_mock.enqueue_feedback(
        [
            {
                "id": "fb1",
                "type": "function",
                "function": {"name": "give_feedback", "arguments": {"suggested_actions": "Reply to the friendly greeting."}},
            }
        ]
    )

    with register_for_test(make_workflow("post_observer", post_pipeline=post_hook)):
        events = await _drain(handle_turn(cid, "hello"))

    assert [name for name, _ in llm_mock.calls if name in ("post_processing", "feedback")] == [
        "post_processing",
        "post_processing",
        "feedback",
    ]
    post_calls = [call for call in llm_mock.captured if call["pass"] == "post_processing"]
    writer_call = next(call for call in llm_mock.captured if call["pass"] == "writer")
    tool_names = [tool["function"]["name"] for tool in writer_call["tools"]]
    assert "editor_find_replace" in tool_names
    # The blob offers the auditor's tool, but its toggle is off, so no audit call runs.
    assert "editor" not in [name for name, _ in llm_mock.calls]
    assert all(call["tools"] == writer_call["tools"] for call in post_calls)
    assert post_calls[0]["messages"][-2]["content"] == "Hello there."
    assert post_calls[1]["messages"][-2]["content"] == "Hey there."
    assert "## First Edit" in post_calls[0]["messages"][-1]["content"]
    assert "## Second Edit" in post_calls[1]["messages"][-1]["content"]

    assert next(call for call in llm_mock.captured if call["pass"] == "feedback")["messages"][-2]["content"] == "Hey, friend."
    assert seen_by_workflow == ["Hey, friend."]

    [writer_done] = [event for event in events if event.get("event") == "writer_done"]
    assert writer_done["data"]["editor_will_run"] is True
    assert [event["data"]["step"] for event in events if event.get("event") == "step_start"] == [
        "writer",
        "post_processing",
        "feedback",
    ]
    assert [event["data"]["draft"] for event in events if event.get("event") == "draft_update"] == [
        "Hey there.",
        "Hey, friend.",
    ]
    assert [event["data"]["refined_text"] for event in events if event.get("event") == "writer_rewrite"] == ["Hey, friend."]
    [editor_done] = [event for event in events if event.get("event") == "editor_done"]
    assert [call["name"] for call in editor_done["data"]["tool_calls"]] == ["editor_find_replace", "editor_find_replace"]
    assert "first edit reasoning" in "".join(
        event["data"]["delta"] for event in events if event.get("event") == "reasoning" and event["data"]["pass"] == "editor"
    )

    assistant = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"][-1]
    assert assistant["content"] == "Hey, friend."
    assert assistant["writer_draft"] == "Hey, friend."


async def test_post_processing_is_skipped_when_agent_is_off(client, llm_mock):
    cid = "conv-post-processing-off"
    await dbmod.create_conversation(cid, "post off", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": False})
    await _create_fragment(client, "would_edit", "Edit it.", 8)
    llm_mock.enqueue_writer("Original.")
    llm_mock.enqueue_post_processing(_call("Original", "Changed", call_id="unused"))

    events = await _drain(handle_turn(cid, "hello"))

    assert not any(name == "post_processing" for name, _ in llm_mock.calls)
    [writer_done] = [event for event in events if event.get("event") == "writer_done"]
    assert writer_done["data"]["editor_will_run"] is False
    assistant = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"][-1]
    assert assistant["content"] == "Original."


async def test_post_processing_receives_output_auditors_edited_draft(client, llm_mock):
    cid = "conv-post-processing-audited"
    await dbmod.create_conversation(cid, "post audited", "Bot", "a scenario")
    await client.put(
        "/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True, "editor_apply_patch": True}}
    )
    await _create_fragment(client, "after_audit", "Soften the first line.", 8)

    llm_mock.enqueue_writer("Her voice was barely a whisper. His answer was barely a whisper.")
    llm_mock.enqueue_editor(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "audit1",
                    "type": "function",
                    "function": {
                        "name": "editor_apply_patch",
                        "arguments": {
                            "patches": [{"id": 1, "replace": "She whispered."}, {"id": 2, "replace": "He whispered back."}]
                        },
                    },
                }
            ],
        }
    )
    llm_mock.enqueue_post_processing(_call("She whispered.", "She spoke softly.", call_id="post1"))

    events = await _drain(handle_turn(cid, "hello"))

    calls = [name for name, _ in llm_mock.calls]
    assert calls.index("editor") < calls.index("post_processing")
    assert [event["data"]["step"] for event in events if event.get("event") == "step_start"] == [
        "writer",
        "output_auditor",
        "post_processing",
    ]
    post_call = next(call for call in llm_mock.captured if call["pass"] == "post_processing")
    assert post_call["messages"][-2]["content"] == "She whispered. He whispered back."
    assistant = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"][-1]
    assert assistant["content"] == "She spoke softly. He whispered back."


async def test_abort_stops_remaining_fragments_feedback_and_workflows(client, llm_mock):
    cid = "conv-post-processing-abort"
    await dbmod.create_conversation(cid, "post abort", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": True})
    await client.put("/api/interactive-fragments/suggested_actions", json={"enabled": True})
    await _create_fragment(client, "first_abort", "First.", 8)
    await _create_fragment(client, "second_abort", "Second.", 9)

    workflow_ran: list[bool] = []

    async def post_hook(ctx):
        workflow_ran.append(True)
        return
        yield  # pragma: no cover

    llm_mock.enqueue_writer("Original.")
    llm_mock.enqueue_post_processing(_call("Original", "Changed", call_id="unused1"))
    llm_mock.enqueue_post_processing(_call("Changed", "Changed again", call_id="unused2"))
    gate = llm_mock.gate("post_processing")

    with register_for_test(make_workflow("abort_observer", post_pipeline=post_hook)):
        task = asyncio.create_task(_drain(handle_turn(cid, "hello")))
        await gate.reached.wait()
        llm_mock.abort()
        gate.release.set()
        events = await task

    assert [name for name, _ in llm_mock.calls].count("post_processing") == 1
    assert not any(name == "feedback" for name, _ in llm_mock.calls)
    assert workflow_ran == []
    assert not [event for event in events if event.get("event") == "writer_rewrite"]
    assistant = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"][-1]
    assert assistant["content"] == "Original."


def test_upgrade_migration_seeds_humanize_dialogue_once():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE interactive_fragments ("
        "id TEXT PRIMARY KEY, label TEXT NOT NULL, description TEXT NOT NULL, "
        "field_type TEXT NOT NULL, required INTEGER NOT NULL, enabled INTEGER NOT NULL, "
        "injection_label TEXT NOT NULL, sort_order INTEGER NOT NULL, "
        "direction_note_timing TEXT NOT NULL DEFAULT 'post_turn')"
    )
    migrate = importlib.import_module("backend.database.migrations.0061_post_processing_fragment").migrate
    migrate(conn)
    migrate(conn)
    rows = conn.execute(
        "SELECT id, label, field_type, required, enabled, injection_label, sort_order FROM interactive_fragments"
    ).fetchall()
    assert rows == [("humanize_dialogue", "Humanize Dialogue", "post_processing", 0, 0, "Humanize Dialogue", 7)]
    conn.close()


class _Judge:
    """A stubbed decisions gateway answering every gate with one probability."""

    def __init__(self, monkeypatch, probability: float):
        self.states: list[str] = []
        judge = self

        async def decide(self, state, questions, *, timeout=None, abort=None):  # noqa: ANN001
            judge.states.append(state)
            return DecisionResponse(answers={question.key: probability for question in questions})

        monkeypatch.setattr(DecisionClient, "decide", decide)


async def _configure_judge(client) -> None:
    endpoint = await client.post_json("/api/endpoints", json={"url": "https://judge.test/api/v1", "kind": "judge"})
    response = await client.put(
        "/api/decisions/config", json={"decision_endpoint_id": endpoint["id"], "decision_model": "typesafe/jev-1.13"}
    )
    assert response.json()["configured"] is True


async def _director_log(client, cid: str, message_id: int) -> dict:
    return await client.get_json(f"/api/conversations/{cid}/messages/{message_id}/director-log")


def _gate_records(calls: list[dict]) -> list[dict]:
    return [call["arguments"] for call in calls if call["name"] == "post_processing_gate"]


async def test_gates_answering_no_skip_every_fragment_but_feedback_and_workflows_still_run(client, llm_mock, monkeypatch):
    cid = "conv-post-processing-gated"
    await dbmod.create_conversation(cid, "gated", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": True})
    await client.put("/api/interactive-fragments/suggested_actions", json={"enabled": True})
    await _configure_judge(client)
    await _create_fragment(client, "trim", "Trim to two actions.", 8, gate="Do more than two actions happen?")
    await _create_fragment(client, "soften", "Soften it.", 9, gate="Is the reply harsh?")
    judge = _Judge(monkeypatch, 0.1)

    seen_by_workflow: list[str] = []

    async def post_hook(ctx):
        seen_by_workflow.append(ctx.draft)
        return
        yield  # pragma: no cover

    llm_mock.enqueue_writer("Mara sat down.")
    llm_mock.enqueue_post_processing(_call("Mara", "Unused", call_id="unused"))
    llm_mock.enqueue_feedback(
        [
            {
                "id": "fb1",
                "type": "function",
                "function": {"name": "give_feedback", "arguments": {"suggested_actions": "Sit too."}},
            }
        ]
    )

    with register_for_test(make_workflow("gate_observer", post_pipeline=post_hook)):
        events = await _drain(handle_turn(cid, "Where is Mara?"))

    assert judge.states == ["Current request:\nWhere is Mara?\n\nReply:\nMara sat down."] * 2
    assert not any(name == "post_processing" for name, _ in llm_mock.calls)
    feedback_call = next(call for call in llm_mock.captured if call["pass"] == "feedback")
    writer_call = next(call for call in llm_mock.captured if call["pass"] == "writer")
    assert feedback_call["messages"][-2]["content"] == "Mara sat down."
    # A skipped fragment changes nothing in the shared tool blob.
    assert "editor_find_replace" in [tool["function"]["name"] for tool in writer_call["tools"]]
    assert json.dumps(feedback_call["tools"]) == json.dumps(writer_call["tools"])
    assert seen_by_workflow == ["Mara sat down."]
    assert [event["data"]["step"] for event in events if event.get("event") == "step_start"] == [
        "writer",
        "post_processing",
        "feedback",
    ]
    assert not [event for event in events if event.get("event") in ("draft_update", "writer_rewrite")]

    [editor_done] = [event for event in events if event.get("event") == "editor_done"]
    expected = [
        {
            "fragment_id": "trim",
            "label": "trim",
            "question": "Do more than two actions happen?",
            "fired": 0,
            "reason": "condition_not_met",
            "probability": 0.1,
        },
        {
            "fragment_id": "soften",
            "label": "soften",
            "question": "Is the reply harsh?",
            "fired": 0,
            "reason": "condition_not_met",
            "probability": 0.1,
        },
    ]
    assert _gate_records(editor_done["data"]["tool_calls"]) == expected
    assistant = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"][-1]
    assert assistant["content"] == "Mara sat down."
    log = await _director_log(client, cid, assistant["id"])
    assert _gate_records(log["tool_calls"]) == expected
    assert log["feedback"] == {"suggested_actions": "Sit too."}


async def test_a_gate_answering_yes_runs_its_fragment(client, llm_mock, monkeypatch):
    cid = "conv-post-processing-gate-yes"
    await dbmod.create_conversation(cid, "gate yes", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": True})
    await _configure_judge(client)
    await _create_fragment(client, "trim", "Trim.", 8, gate="Do more than two actions happen?")
    _Judge(monkeypatch, 0.9)
    llm_mock.enqueue_writer("Mara sat, stood, and ran.")
    llm_mock.enqueue_post_processing(_call("sat, stood, and ran", "ran", call_id="p1"))

    events = await _drain(handle_turn(cid, "go"))

    [editor_done] = [event for event in events if event.get("event") == "editor_done"]
    assert [call["name"] for call in editor_done["data"]["tool_calls"]] == ["post_processing_gate", "editor_find_replace"]
    assistant = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"][-1]
    assert assistant["content"] == "Mara ran."


async def test_agent_off_turns_ask_no_gates(client, llm_mock, monkeypatch):
    cid = "conv-post-processing-gate-off"
    await dbmod.create_conversation(cid, "gate off", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": False})
    await _configure_judge(client)
    await _create_fragment(client, "trim", "Trim.", 8, gate="Do more than two actions happen?")
    judge = _Judge(monkeypatch, 0.9)
    llm_mock.enqueue_writer("Original.")

    await _drain(handle_turn(cid, "hello"))

    assert judge.states == []
    assert not any(name == "post_processing" for name, _ in llm_mock.calls)


def _sse_events(body: str) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    name = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            try:
                data: object = json.loads(line[6:])
            except json.JSONDecodeError:
                data = line[6:]
            events.append((name, data))
    return events


async def test_each_group_reply_is_gated_on_its_own_draft(client, llm_mock, monkeypatch):
    cards = [await client.create("/api/characters", json={"name": name}) for name in ("Aria", "Kael")]
    conv = await client.post_json(
        "/api/conversations",
        json={"kind": "group", "title": "Camp", "members": [{"character_card_id": card} for card in cards]},
    )
    await client.put("/api/settings", json={"enable_agent": True})
    await _configure_judge(client)
    await _create_fragment(client, "trim", "Trim.", 8, gate="Do more than two actions happen?")
    judge = _Judge(monkeypatch, 0.1)
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
    llm_mock.enqueue_writer("I found tracks.")
    llm_mock.enqueue_writer("The ward is broken.")

    response = await client.post_checked(f"/api/conversations/{conv['id']}/send", json={"content": "What happened?"})

    assert not any(name == "post_processing" for name, _ in llm_mock.calls)
    assert [state.rsplit("Reply:\n", 1)[1] for state in judge.states] == ["I found tracks.", "The ward is broken."]
    done = [data for name, data in _sse_events(response.text) if name == "editor_done"]
    assert [[call["name"] for call in data["tool_calls"]] for data in done] == [["post_processing_gate"]] * 2  # type: ignore[index]
    replies = [message for message in await dbmod.get_messages(conv["id"]) if message["role"] == "assistant"]
    assert [reply["content"] for reply in replies] == ["I found tracks.", "The ward is broken."]
    for reply in replies:
        log = await _director_log(client, conv["id"], reply["id"])
        assert [record["reason"] for record in _gate_records(log["tool_calls"])] == ["condition_not_met"]
