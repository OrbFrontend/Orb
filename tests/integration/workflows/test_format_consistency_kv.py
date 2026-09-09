"""Integration tests for the standalone voice-rewrite lane."""

from __future__ import annotations

import json

import pytest

from backend.database import set_workflow_enabled
from backend.workflows import set_workflow_config
from backend.workflows.format_consistency import (
    VOICE_REWRITE_LENGTH_RULE,
    VOICE_REWRITE_TOOL_NAME,
    hooks,
    voice,
)

# Third/past baseline, second/present draft -> a drift the hook must repair.
BASELINE = "She smiles and steps back. The woods were quiet that evening."
DRIFTING_DRAFT = "You step closer, watching her carefully."
REWRITTEN = "She stepped closer, watching her carefully."


def names_of(call) -> list[str]:
    return [t["function"]["name"] for t in call["tools"] or []]


def _wire(obj) -> str:
    """The bytes the server's prefix matcher sees (insertion order, no sorting)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture
def voice_on(monkeypatch):
    """Classifier present, and answering so the draft drifts from the baseline."""
    monkeypatch.setattr(hooks, "local_feature_ready", lambda feature, settings: True)

    async def classify(text: str) -> tuple[str, str]:
        return ("second", "present") if text.startswith("You ") else ("third", "past")

    monkeypatch.setattr(voice, "classify_pov_tense", classify)


async def _seed(client) -> str:
    card = await client.post(
        "/api/characters",
        json={"name": "Aria", "description": "An elf ranger.", "first_mes": BASELINE},
    )
    assert card.status_code == 200
    conv = await client.post("/api/conversations", json={"character_card_id": card.json()["id"]})
    assert conv.status_code == 200

    resp = await client.put(
        "/api/settings",
        json={
            "model_name": "writer-model",
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True, "editor_apply_patch": False},
            "length_guard_enabled": False,
        },
    )
    assert resp.status_code == 200

    await set_workflow_enabled("format_consistency", True)
    await set_workflow_config("format_consistency", {"voice_consistency": True})
    return conv.json()["id"]


async def test_voice_rewrite_carries_no_conversation(client, llm_mock, voice_on):
    cid = await _seed(client)

    llm_mock.enqueue_director([{"id": "c1", "type": "function", "function": {"name": "direct_scene", "arguments": "{}"}}])
    llm_mock.enqueue_writer(DRIFTING_DRAFT)
    llm_mock.enqueue_workflow(
        {
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {
                        "name": VOICE_REWRITE_TOOL_NAME,
                        "arguments": json.dumps({"rewritten_text": REWRITTEN}),
                    },
                }
            ]
        }
    )

    send = await client.post(f"/api/conversations/{cid}/send", json={"content": "and then?", "attachments": []})
    assert send.status_code == 200
    _ = send.text

    by_pass = {c["pass"]: c for c in llm_mock.captured}
    assert "workflow" in by_pass, f"voice rewrite never fired (passes: {sorted(by_pass)})"

    rewrite = by_pass["workflow"]
    director = by_pass["director"]
    assert rewrite["tool_choice"] == {"type": "function", "function": {"name": VOICE_REWRITE_TOOL_NAME}}

    assert names_of(rewrite) == [VOICE_REWRITE_TOOL_NAME]
    assert VOICE_REWRITE_LENGTH_RULE in rewrite["tools"][0]["function"]["description"]

    assert _wire(rewrite["messages"][0]) != _wire(director["messages"][0])
    assert len(rewrite["messages"]) == 2
    assert [m["role"] for m in rewrite["messages"]] == ["system", "user"]
    assert DRIFTING_DRAFT in rewrite["messages"][1]["content"]
    assert BASELINE not in _wire(rewrite["messages"])

    assert len(_wire(rewrite["messages"])) < len(_wire(director["messages"]))


async def test_voice_off_leaves_the_turn_untouched(client, llm_mock, voice_on):
    """Voice enforcement is opt-in and invisible to the turn when disabled."""
    cid = await _seed(client)
    await set_workflow_config("format_consistency", {"voice_consistency": False})

    llm_mock.enqueue_director([{"id": "c1", "type": "function", "function": {"name": "direct_scene", "arguments": "{}"}}])
    llm_mock.enqueue_writer(DRIFTING_DRAFT)

    send = await client.post(f"/api/conversations/{cid}/send", json={"content": "and then?", "attachments": []})
    assert send.status_code == 200
    _ = send.text

    by_pass = {c["pass"]: c for c in llm_mock.captured}
    assert "workflow" not in by_pass
    assert VOICE_REWRITE_TOOL_NAME not in names_of(by_pass["director"])


async def test_the_rewrite_prompt_does_not_grow_with_history(client, llm_mock, voice_on):
    """The rewrite prompt stays constant as conversation history grows."""
    cid = await _seed(client)

    def _queue_turn() -> None:
        llm_mock.enqueue_director([{"id": "c1", "type": "function", "function": {"name": "direct_scene", "arguments": "{}"}}])
        llm_mock.enqueue_writer(DRIFTING_DRAFT)
        llm_mock.enqueue_workflow(
            {
                "tool_calls": [
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {
                            "name": VOICE_REWRITE_TOOL_NAME,
                            "arguments": json.dumps({"rewritten_text": REWRITTEN}),
                        },
                    }
                ]
            }
        )

    rewrites = []
    for msg in ("and then?", "what happens next?"):
        _queue_turn()
        start = len(llm_mock.captured)
        send = await client.post(f"/api/conversations/{cid}/send", json={"content": msg, "attachments": []})
        assert send.status_code == 200
        _ = send.text
        rewrites.append(next(c for c in llm_mock.captured[start:] if c["pass"] == "workflow"))

    first, second = rewrites
    directors = [c for c in llm_mock.captured if c["pass"] == "director"]
    assert len(_wire(directors[1]["messages"])) > len(_wire(directors[0]["messages"]))
    assert _wire(second["messages"]) == _wire(first["messages"])
    assert _wire(second["tools"]) == _wire(first["tools"])
