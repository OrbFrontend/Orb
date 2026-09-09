"""The format_consistency voice rewrite runs on its own lane, not the turn's.

This is the call site the default-on ``verify_kv_prefix_invariants`` teardown
never saw: the voice half's only other coverage is a unit test that monkeypatches
``forced_tool_call`` away, so the rewrite never reached ``llm_mock``. That is how
it shipped forcing ``editor_rewrite`` against the turn's tool blob without being
in it -- ``forced_tool_call`` appended the schema, and the tools region renders
ahead of history, so the 557 appended bytes evicted the whole conversation from
the server's prefix cache.

A self-contained lane makes that unrepresentable rather than merely fixed: the
call shares no prefix and no tools array with the turn, so it has nothing to
diverge from, and it stops paying a full conversation's prompt tokens to restate
one draft. The checker groups calls by conversation identity (``messages[1]``),
so this lane lands in a group of one and is skipped -- which is why the shape is
asserted here explicitly rather than left to the teardown.
"""

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

    # Agent on with the Director's tool, length guard OFF -- the configuration
    # that used to leave editor_rewrite out of the blob.
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
    # The rewrite actually ran; without it the rest of this proves nothing.
    assert "workflow" in by_pass, f"voice rewrite never fired (passes: {sorted(by_pass)})"

    rewrite = by_pass["workflow"]
    director = by_pass["director"]
    assert rewrite["tool_choice"] == {"type": "function", "function": {"name": VOICE_REWRITE_TOOL_NAME}}

    # Only the forced tool. Shipping the turn's blob is what used to append a
    # schema the Director and Writer had not sent.
    assert names_of(rewrite) == [VOICE_REWRITE_TOOL_NAME]
    assert VOICE_REWRITE_LENGTH_RULE in rewrite["tools"][0]["function"]["description"]

    # Its own prefix, and a short one: no system message, history or scene from
    # the turn. This is the token saving and the divergence-proofing at once.
    assert _wire(rewrite["messages"][0]) != _wire(director["messages"][0])
    assert len(rewrite["messages"]) == 2
    assert [m["role"] for m in rewrite["messages"]] == ["system", "user"]
    assert DRIFTING_DRAFT in rewrite["messages"][1]["content"]
    assert BASELINE not in _wire(rewrite["messages"])

    assert len(_wire(rewrite["messages"])) < len(_wire(director["messages"]))


async def test_voice_off_leaves_the_turn_untouched(client, llm_mock, voice_on):
    """Opt-in, and invisible to the turn when off: no extra call, no extra schema.

    The workflow's standalone schema never enters the turn's blob. The old shared
    editor_rewrite schema was also absent here because the length guard is off,
    which is the configuration where the append used to fire.
    """
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
    """Two turns, same drifting draft, byte-identical rewrite prompts.

    The load-bearing property, and the one a size comparison only gestures at: the
    call is closed over the draft, so a conversation ten turns deep sends the same
    bytes as one turn deep. That is what makes the saving scale with the thing that
    was expensive -- and it is what silently regresses the moment someone reaches
    for the turn prefix or ``ctx.history`` to give the rewriter "context".
    """
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
    # The second turn's Director has grown; the second turn's rewrite has not.
    directors = [c for c in llm_mock.captured if c["pass"] == "director"]
    assert len(_wire(directors[1]["messages"])) > len(_wire(directors[0]["messages"]))
    assert _wire(second["messages"]) == _wire(first["messages"])
    assert _wire(second["tools"]) == _wire(first["tools"])
