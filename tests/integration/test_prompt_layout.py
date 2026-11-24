"""Prompt layout: the prefix stays byte-stable around Writer-request edits, and every builder shares one layout."""

import json

from backend.core import Macros
from backend.core.prompt_layout import LayoutBlock, default_layout
from backend.database import add_message, create_conversation, get_messages, get_settings, set_active_leaf
from backend.pipeline.context import build_prefixes, load_pipeline_context
from backend.prompting import build_prefix
from backend.workflows.toolkit import build_offturn_prefix

_LONG_DRAFT = "word " * 60


def _wire(messages) -> str:
    return "\n".join(json.dumps(m, separators=(",", ":"), sort_keys=True) for m in messages)


async def _active_layout(client, name: str = "Custom") -> tuple[str, dict[str, dict]]:
    layout = await client.post_json("/api/prompt-layouts", json={"name": name})
    await client.put_checked("/api/prompt-layouts/active", json={"id": layout["id"]})
    blocks = await client.get_json(f"/api/prompt-layouts/{layout['id']}/blocks")
    return layout["id"], {block["kind"]: block for block in blocks}


async def _add_block(client, layout_id: str, **body) -> dict:
    return await client.post_json(f"/api/prompt-layouts/{layout_id}/blocks", json=body)


async def _card_conversation(client) -> str:
    card = await client.post_json(
        "/api/characters",
        json={
            "name": "Aria",
            "description": "{{char}} guards {{user}}.",
            "first_mes": "Greetings, {{user}}.",
            "scenario": "Deep woods.",
        },
    )
    return await client.create("/api/conversations", json={"character_card_id": card["id"]})


async def test_writer_blocks_leave_the_prefix_alone_and_the_editor_replays_them(client, llm_mock):
    await client.put_checked(
        "/api/settings",
        json={"enable_agent": True, "length_guard_enabled": True, "length_guard_max_words": 5},
    )
    layout_id, _ = await _active_layout(client)
    await _add_block(client, layout_id, content="Rule {{random::a::b::c::d::e::f}}", region="prefix")
    note = await _add_block(client, layout_id, content="Note {{random::1::2::3::4::5::6::7::8}}", region="writer")
    other = await _add_block(client, layout_id, content="Second note", region="writer")
    cid = await _card_conversation(client)

    llm_mock.enqueue_writer(_LONG_DRAFT)
    llm_mock.enqueue_editor(None)
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Hi", "attachments": []})).text
    first = [call for call in llm_mock.captured if call["pass"] == "writer"][-1]["messages"]
    editor = [call for call in llm_mock.captured if call["pass"] == "editor"][-1]["messages"]

    await client.expect("patch", f"/api/prompt-blocks/{note['id']}", json={"content": "Edited {{random::x::y}}"})
    await client.expect("patch", f"/api/prompt-blocks/{other['id']}", json={"enabled": False})
    llm_mock.enqueue_writer(_LONG_DRAFT)
    llm_mock.enqueue_editor(None)
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Again", "attachments": []})).text
    second = [call for call in llm_mock.captured if call["pass"] == "writer"][-1]["messages"]

    # The Writer request is one user message after the shared prefix, and the next turn's request extends that prefix.
    assert first[-1]["role"] == "user" and isinstance(first[-1]["content"], str)
    assert "{{random" not in first[-1]["content"]
    assert _wire(second[: len(first) - 1]) == _wire(first[:-1])
    # The Editor replays the Writer's request verbatim, rolls included.
    assert editor[: len(first)] == first


async def test_offturn_prefix_matches_the_turn_prefix_under_a_custom_layout(client):
    layout_id, markers = await _active_layout(client)
    await _add_block(client, layout_id, content="House rules for {{user}}.", region="prefix")
    await _add_block(client, layout_id, content="Previously {{random::a::b::c}}", region="prefix", role="assistant")
    await client.expect("patch", f"/api/prompt-blocks/{markers['scenario']['id']}", json={"content": "## Place\n{{content}}"})
    await client.expect("patch", f"/api/prompt-blocks/{markers['user_persona']['id']}", json={"enabled": False})
    cid = "layout-parity"
    await create_conversation(cid, "Parity", "Iris", "A rainy archive.", post_history_instructions="Stay in character.")
    mid, _ = await add_message(cid, "assistant", "She looks up.", 0)
    mid, _ = await add_message(cid, "user", "Hello.", 1, parent_id=mid)
    await set_active_leaf(cid, mid)
    order = [block for block in await client.get_json(f"/api/prompt-layouts/{layout_id}/blocks")]
    await client.put_checked(
        f"/api/prompt-layouts/{layout_id}/order",
        json={
            "prefix": [b["id"] for b in order if b["region"] == "prefix" and b["kind"] != "card_instructions"],
            "writer": [b["id"] for b in order if b["region"] == "writer"] + [markers["card_instructions"]["id"]],
        },
    )

    history = await get_messages(cid)
    ctx = await load_pipeline_context(cid)
    assert ctx is not None
    turn, _ = build_prefixes(ctx, history)
    offturn = await build_offturn_prefix(cid, history, await get_settings(), lane="agent")

    assert [m["role"] for m in turn] == ["system", "assistant", "user"]
    assert _wire(offturn) == _wire(turn)
    assert _wire(build_prefixes(ctx, history)[0]) == _wire(turn)


def test_pre_history_assistant_block_merges_into_an_assistant_greeting():
    layout = (
        *default_layout(),
        LayoutBlock(id="a", kind="custom", content="One", role="assistant", sort_order=20),
        LayoutBlock(id="b", kind="custom", content="Two", role="assistant", sort_order=21),
    )
    history = [{"role": "assistant", "content": "Greeting"}, {"role": "user", "content": "Hi"}]
    prefix = build_prefix("Sys", "", "", messages=history, macros=Macros("U", "C", seed="s"), layout=layout)

    assert [m["role"] for m in prefix] == ["system", "assistant", "user"]
    assert prefix[1]["content"] == "One\n\nTwo\n\nGreeting"


async def test_summary_ignores_the_active_layout(client, llm_mock):
    cid = "layout-summary"
    await create_conversation(cid, "Story", "Bot", "scenario", post_history_instructions="Keep it short.")
    parent = None
    for index in range(6):
        parent, _ = await add_message(cid, ("user", "assistant")[index % 2], f"line {index}", index, parent_id=parent)
    await set_active_leaf(cid, parent)
    body = {"keep_count": 2}

    llm_mock.enqueue_writer("Default take.")
    await client.post_checked(f"/api/conversations/{cid}/summarize", json=body)
    layout_id, markers = await _active_layout(client)
    await _add_block(client, layout_id, content="Never summarized.", region="prefix", role="user")
    await client.expect("patch", f"/api/prompt-blocks/{markers['scenario']['id']}", json={"enabled": False})
    llm_mock.enqueue_writer("Custom take.")
    await client.post_checked(f"/api/conversations/{cid}/summarize", json=body)

    default_call, custom_call = (call["messages"] for call in llm_mock.captured[-2:])
    assert _wire(custom_call) == _wire(default_call)
