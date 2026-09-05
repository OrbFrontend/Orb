"""Integration coverage for pass reasoning buffers."""

from __future__ import annotations

import backend.database as dbmod
from backend.pipeline import handle_turn

_SCENE_THOUGHT = "Evaluating whether the exchange contains user-specific facts suitable for the lorebook."
_NOTE_THOUGHT = "Recording user-stated personal claims as durable lorebook facts."
_NOTE = "The user says he grew up in Lisbon."


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


def _streamed(events: list[dict], pass_name: str) -> str:
    return "".join(e["data"]["delta"] for e in events if e.get("event") == "reasoning" and e["data"]["pass"] == pass_name)


async def test_a_sub_step_opens_a_new_paragraph_in_its_pass_box(client, db, llm_mock):
    cid = "conv-reasoning-seam"
    await dbmod.create_conversation(cid, "seam", "Bot", "a scenario")
    await dbmod.create_interactive_fragment(
        {
            "id": "trajectory",
            "label": "Trajectory",
            "description": "Record the direction of travel.",
            "field_type": "direction_note",
            "injection_label": "Direction of travel",
            "enabled": True,
            "direction_note_timing": "pre_writer",
        }
    )
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "direction_notes_record": True,
            "enabled_tools": {"direct_scene": True},
            "reasoning_enabled_passes": {"director": True, "writer": True, "editor": True},
        },
    )

    # The scene call and note sub-step share the Director buffer.
    llm_mock.enqueue_reasoning("director", _SCENE_THOUGHT)
    llm_mock.enqueue_director([{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": []}}}])
    llm_mock.enqueue_reasoning("direction_note", _NOTE_THOUGHT)
    llm_mock.enqueue_direction_note(
        [{"type": "function", "function": {"name": "record_direction_note", "arguments": {"trajectory": _NOTE}}}]
    )
    llm_mock.enqueue_reasoning("writer", "Answering in her voice.")
    llm_mock.enqueue_writer("She nods slowly.")

    events = await _drain(handle_turn(cid, "hello"))

    assert _streamed(events, "director") == f"{_SCENE_THOUGHT}\n\n{_NOTE_THOUGHT}"
    # The writer has only one call.
    assert _streamed(events, "writer") == "Answering in her voice."

    # Persistence matches the stream.
    log = (await dbmod.get_conversation_logs(cid))[-1]
    assert log["reasoning_director"] == f"{_SCENE_THOUGHT}\n\n{_NOTE_THOUGHT}"
    assert log["reasoning_writer"] == "Answering in her voice."
