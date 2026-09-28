"""A disabled fragment's text reaches no model call.

The shared tools blob offers every defined fragment so an enable toggle never
rewrites the cached prefix, but it offers them by name and type only. In
single-model mode the Writer runs on the Director's lane, tools included, so a
description on the blob would steer the reply toward what the user disabled the
fragment to avoid.
"""

from __future__ import annotations

import json

import backend.database as dbmod
from backend.database.seeds import SEED_INTERACTIVE_FRAGMENTS
from backend.pipeline import handle_turn

_DESCRIPTIONS = {row["id"]: row["description"] for row in SEED_INTERACTIVE_FRAGMENTS if "description" in row}
_TONE_CHECK = "Flag any line where the narration's tone clashes with the scene."


def _call(name: str, **args) -> list[dict]:
    return [{"type": "function", "function": {"name": name, "arguments": args}}]


def _requests(llm_mock, pass_name: str) -> list[str]:
    """Every byte a pass sent: messages, tools, and params (``json_schema`` included)."""
    return [json.dumps(c, default=str, ensure_ascii=False) for c in llm_mock.captured if c["pass"] == pass_name]


async def test_disabled_fragment_description_reaches_no_pass(client, db, llm_mock):
    cid = "conv-disabled-fragment-text"
    await dbmod.create_conversation(cid, "text", "Bot", "a scenario")
    resp = await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})
    assert resp.status_code == 200, resp.text
    for fid, enabled in (("user_intent", False), ("suggested_actions", True), ("characterization", True)):
        resp = await client.put(f"/api/interactive-fragments/{fid}", json={"enabled": enabled})
        assert resp.status_code == 200, resp.text
    # A disabled feedback fragment beside a live one: the feedback call is where it would leak.
    tone_check = {"id": "tone_check", "label": "Tone", "description": _TONE_CHECK, "field_type": "feedback"}
    resp = await client.post("/api/interactive-fragments", json={**tone_check, "enabled": False, "injection_label": "Tone"})
    assert resp.status_code == 200, resp.text

    llm_mock.enqueue_director(_call("direct_scene", moods=[], keywords=["lantern"], next_event="Rain falls."))
    llm_mock.enqueue_writer("She lights the lantern.")
    llm_mock.enqueue_feedback(_call("give_feedback", suggested_actions="Ask about the rain."))
    llm_mock.enqueue_state(_call("update_state", characterization=[]))

    _ = [event async for event in handle_turn(cid, "hello")]

    passes = {c["pass"] for c in llm_mock.captured}
    assert {"director", "writer", "feedback", "state"} <= passes
    writer_tools = [json.dumps(c["tools"]) for c in llm_mock.captured if c["pass"] == "writer"]
    assert writer_tools and all('"user_intent"' in tools and '"tone_check"' in tools for tools in writer_tools)
    disabled = (json.dumps(_DESCRIPTIONS["user_intent"], ensure_ascii=False)[1:-1], _TONE_CHECK)
    for pass_name in passes:
        for request in _requests(llm_mock, pass_name):
            assert not any(text in request for text in disabled), pass_name

    # A live field's description reaches the pass that fills it, and only that pass.
    keywords = json.dumps(_DESCRIPTIONS["keywords"], ensure_ascii=False)[1:-1]
    assert any(keywords in request for request in _requests(llm_mock, "director"))
    assert not any(keywords in request for request in _requests(llm_mock, "writer"))
    suggestions = json.dumps(_DESCRIPTIONS["suggested_actions"], ensure_ascii=False)[1:-1]
    assert any(suggestions in request for request in _requests(llm_mock, "feedback"))
    assert not any(suggestions in request for request in _requests(llm_mock, "writer"))
