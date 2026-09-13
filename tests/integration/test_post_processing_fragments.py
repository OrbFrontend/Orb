"""Conversation-turn coverage for post-processing interactive fragments."""

from __future__ import annotations

import asyncio
import importlib
import sqlite3

import backend.database as dbmod
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
            "function": {
                "name": "editor_search_replace",
                "arguments": {"patches": [{"search": search, "replace": replace}]},
            },
        }
    ]


async def _create_fragment(client, fid: str, instruction: str, sort_order: int) -> None:
    response = await client.post(
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
        },
    )
    assert response.status_code == 200, response.text


async def test_ordered_fragments_edit_before_feedback_workflow_and_persistence(client, llm_mock):
    cid = "conv-post-processing"
    await dbmod.create_conversation(cid, "post", "Bot", "a scenario")
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "feedback_enabled": True,
            "reasoning_enabled_passes": {"director": False, "writer": False, "editor": True},
        },
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
                "function": {
                    "name": "give_feedback",
                    "arguments": {"suggested_actions": "Reply to the friendly greeting."},
                },
            }
        ]
    )

    workflow = make_workflow("post_observer", post_pipeline=post_hook)
    with register_for_test(workflow):
        events = await _drain(handle_turn(cid, "hello"))

    assert [name for name, _ in llm_mock.calls if name in ("post_processing", "feedback")] == [
        "post_processing",
        "post_processing",
        "feedback",
    ]
    post_calls = [call for call in llm_mock.captured if call["pass"] == "post_processing"]
    writer_call = next(call for call in llm_mock.captured if call["pass"] == "writer")
    tool_names = [tool["function"]["name"] for tool in writer_call["tools"]]
    assert "editor_search_replace" in tool_names
    assert "editor_apply_patch" not in tool_names
    assert all(call["tools"] == writer_call["tools"] for call in post_calls)
    assert post_calls[0]["messages"][-2]["content"] == "Hello there."
    assert post_calls[1]["messages"][-2]["content"] == "Hey there."
    assert "## First Edit" in post_calls[0]["messages"][-1]["content"]
    assert "## Second Edit" in post_calls[1]["messages"][-1]["content"]

    feedback_call = next(call for call in llm_mock.captured if call["pass"] == "feedback")
    assert feedback_call["messages"][-2]["content"] == "Hey, friend."
    assert seen_by_workflow == ["Hey, friend."]

    [writer_done] = [event for event in events if event.get("event") == "writer_done"]
    assert writer_done["data"]["editor_will_run"] is True
    assert [event["data"]["draft"] for event in events if event.get("event") == "draft_update"] == [
        "Hey there.",
        "Hey, friend.",
    ]
    assert [event["data"]["refined_text"] for event in events if event.get("event") == "writer_rewrite"] == ["Hey, friend."]
    [editor_done] = [event for event in events if event.get("event") == "editor_done"]
    assert [call["name"] for call in editor_done["data"]["tool_calls"]] == [
        "editor_search_replace",
        "editor_search_replace",
    ]
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
        "/api/settings",
        json={
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True, "editor_apply_patch": True},
        },
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
                            "patches": [
                                {"id": 1, "replace": "She whispered."},
                                {"id": 2, "replace": "He whispered back."},
                            ]
                        },
                    },
                }
            ],
        }
    )
    llm_mock.enqueue_post_processing(_call("She whispered.", "She spoke softly.", call_id="post1"))

    await _drain(handle_turn(cid, "hello"))

    calls = [name for name, _ in llm_mock.calls]
    assert calls.index("editor") < calls.index("post_processing")
    post_call = next(call for call in llm_mock.captured if call["pass"] == "post_processing")
    assert post_call["messages"][-2]["content"] == "She whispered. He whispered back."
    assistant = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"][-1]
    assert assistant["content"] == "She spoke softly. He whispered back."


async def test_abort_stops_remaining_fragments_feedback_and_workflows(client, llm_mock):
    cid = "conv-post-processing-abort"
    await dbmod.create_conversation(cid, "post abort", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": True, "feedback_enabled": True})
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

    workflow = make_workflow("abort_observer", post_pipeline=post_hook)
    with register_for_test(workflow):
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
