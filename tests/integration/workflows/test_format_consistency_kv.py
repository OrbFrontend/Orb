"""Integration tests for the standalone voice-rewrite lane."""

import json

import pytest

from backend.api.routes import messages as message_routes
from backend.database import add_message, get_messages, set_workflow_enabled
from backend.workflows import prose_rewriter_host, set_workflow_config
from backend.workflows.format_consistency import VOICE_REWRITE_LENGTH_RULE, VOICE_REWRITE_TOOL_NAME, hooks, voice

from ._fixtures import make_workflow, register_for_test

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

    async def classify_chunks(text: str) -> list[tuple[str, str]]:
        return [("second", "present") if text.startswith("You ") else ("third", "past")]

    monkeypatch.setattr(voice, "classify_pov_tense_chunks", classify_chunks)


async def _seed(client) -> str:
    card = await client.post_json(
        "/api/characters", json={"name": "Aria", "description": "An elf ranger.", "first_mes": BASELINE}
    )
    conv = await client.post_json("/api/conversations", json={"character_card_id": card["id"]})

    await client.put_checked(
        "/api/settings",
        json={
            "model_name": "writer-model",
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True, "editor_apply_patch": False},
            "length_guard_enabled": False,
        },
    )

    await set_workflow_enabled("format_consistency", True)
    await set_workflow_config("format_consistency", {"voice_consistency": True})
    return conv["id"]


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
                    "function": {"name": VOICE_REWRITE_TOOL_NAME, "arguments": json.dumps({"rewritten_text": REWRITTEN})},
                }
            ]
        }
    )

    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "and then?", "attachments": []})).text

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
    # The newest in-voice reply is the one reference; the turn itself is not sent.
    assert _wire(rewrite["messages"]).count(BASELINE) == 1
    assert "and then?" not in _wire(rewrite["messages"])

    assert len(_wire(rewrite["messages"])) < len(_wire(director["messages"]))


@pytest.mark.parametrize("manual", [False, True])
async def test_voice_repair_runs_once_before_local_prose_rewrite(client, llm_mock, voice_on, monkeypatch, manual):
    cid = await _seed(client)
    config = {"variant_id": "test", "gpu": False, "batch_size": 1}
    sources: list[str] = []
    downstream: list[str] = []
    polished = "She moved closer, watching her carefully."

    async def prose_rewrite(source, _config):
        sources.append(source)
        yield {"type": "rewritten", "draft": polished}

    async def artifact_hook(ctx):
        downstream.append(ctx.draft)
        if False:
            yield  # pragma: no cover -- generator shape

    monkeypatch.setattr(prose_rewriter_host, "resolve_config", lambda _settings: config)
    monkeypatch.setattr(prose_rewriter_host, "rewrite_events", prose_rewrite)
    monkeypatch.setattr(message_routes, "resolve_config", lambda _settings: config)
    monkeypatch.setattr(message_routes, "rewrite_events", prose_rewrite)
    llm_mock.enqueue_workflow(
        {
            "tool_calls": [
                {
                    "id": "voice",
                    "type": "function",
                    "function": {"name": VOICE_REWRITE_TOOL_NAME, "arguments": json.dumps({"rewritten_text": REWRITTEN})},
                }
            ]
        }
    )
    if manual:
        # Manual-only mode must still use the same order, without dispatching
        # automatic prose or artifact hooks while preparing the source.
        await set_workflow_config("prose_rewriter", {"automatic": False})
        history = await get_messages(cid)
        mid, _ = await add_message(
            cid,
            "assistant",
            DRIFTING_DRAFT,
            1,
            parent_id=history[-1]["id"],
            writer_draft=DRIFTING_DRAFT,
            advance_leaf=True,
        )
        url, body = f"/api/conversations/{cid}/messages/{mid}/prose-rewrite", {}
    else:
        llm_mock.enqueue_director([{"id": "c1", "type": "function", "function": {"name": "direct_scene", "arguments": "{}"}}])
        llm_mock.enqueue_writer(DRIFTING_DRAFT)
        url, body = f"/api/conversations/{cid}/send", {"content": "and then?", "attachments": []}

    with register_for_test(make_workflow("after_prose", post_pipeline=artifact_hook)):
        response = await client.post_checked(url, json=body)

    assert sources == [REWRITTEN]
    assert len([call for call in llm_mock.captured if call["pass"] == "workflow"]) == 1
    assert downstream == ([] if manual else [polished])
    assert (await get_messages(cid))[-1]["content"] == polished
    if not manual:
        assert response.text.index('"channel": "workflow:format_consistency", "state": "done"') < response.text.index(
            '"channel": "workflow:prose_rewriter", "label": "Rewriting prose'
        )


async def test_voice_off_leaves_the_turn_untouched(client, llm_mock, voice_on):
    """Voice enforcement is opt-in and invisible to the turn when disabled."""
    cid = await _seed(client)
    await set_workflow_config("format_consistency", {"voice_consistency": False})

    llm_mock.enqueue_director([{"id": "c1", "type": "function", "function": {"name": "direct_scene", "arguments": "{}"}}])
    llm_mock.enqueue_writer(DRIFTING_DRAFT)

    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "and then?", "attachments": []})).text

    by_pass = {c["pass"]: c for c in llm_mock.captured}
    assert "workflow" not in by_pass
    assert VOICE_REWRITE_TOOL_NAME not in names_of(by_pass["director"])


async def test_the_rewrite_prompt_does_not_grow_with_history(client, llm_mock, voice_on):
    """The rewrite prompt does not grow with history: only its reference moves."""
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
                        "function": {"name": VOICE_REWRITE_TOOL_NAME, "arguments": json.dumps({"rewritten_text": REWRITTEN})},
                    }
                ]
            }
        )

    rewrites = []
    for msg in ("and then?", "what happens next?"):
        _queue_turn()
        start = len(llm_mock.captured)
        _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": msg, "attachments": []})).text
        rewrites.append(next(c for c in llm_mock.captured[start:] if c["pass"] == "workflow"))

    first, second = rewrites
    directors = [c for c in llm_mock.captured if c["pass"] == "director"]
    assert len(_wire(directors[1]["messages"])) > len(_wire(directors[0]["messages"]))
    assert _wire(second["messages"][0]) == _wire(first["messages"][0])
    # Turn one's repaired reply is now the newest in-voice row and replaces the greeting.
    assert second["messages"][1]["content"] == first["messages"][1]["content"].replace(BASELINE, REWRITTEN)
    assert _wire(second["tools"]) == _wire(first["tools"])
