"""Prompt layout: the prefix stays byte-stable around Writer-request edits, and every builder shares one layout."""

import json

from backend.core import Macros
from backend.core.prompt_layout import LayoutBlock, default_layout
from backend.database import (
    add_message,
    create_conversation,
    get_conversation,
    get_messages,
    get_settings,
    resolve_cast,
    set_active_leaf,
)
from backend.pipeline.context import build_turn_prefix, load_pipeline_context
from backend.prompting import build_prefix, member_macros, persona_macros
from backend.workflows.toolkit import build_offturn_prefix

_LONG_DRAFT = "word " * 60


def _wire(messages) -> str:
    return "\n".join(json.dumps(m, separators=(",", ":"), sort_keys=True) for m in messages)


async def _active_layout(client, name: str = "Custom") -> tuple[str, dict[str, dict]]:
    """The new active layout's id and its seeded blocks, markers by kind and section blocks by label."""
    layout = await client.post_json("/api/prompt-layouts", json={"name": name})
    await client.put_checked("/api/prompt-layouts/active", json={"id": layout["id"]})
    blocks = await client.get_json(f"/api/prompt-layouts/{layout['id']}/blocks")
    return layout["id"], {block["label"] if block["kind"] == "custom" else block["kind"]: block for block in blocks}


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
    layout_id, seeded = await _active_layout(client)
    await _add_block(client, layout_id, content="House rules for {{user}}.", region="prefix")
    await _add_block(client, layout_id, content="Previously {{random::a::b::c}}", region="prefix", role="assistant")
    await client.expect("patch", f"/api/prompt-blocks/{seeded['User persona']['id']}", json={"enabled": False})
    instructions = seeded["Chat instructions"]["id"]
    cid = "layout-parity"
    await create_conversation(cid, "Parity", "Iris", "A rainy archive.", post_history_instructions="Stay in character.")
    mid, _ = await add_message(cid, "assistant", "She looks up.", 0)
    mid, _ = await add_message(cid, "user", "Hello.", 1, parent_id=mid)
    await set_active_leaf(cid, mid)
    order = [block for block in await client.get_json(f"/api/prompt-layouts/{layout_id}/blocks")]
    await client.put_checked(
        f"/api/prompt-layouts/{layout_id}/order",
        json={
            "prefix": [b["id"] for b in order if b["region"] == "prefix" and b["id"] != instructions],
            "writer": [b["id"] for b in order if b["region"] == "writer"] + [instructions],
        },
    )

    history = await get_messages(cid)
    ctx = await load_pipeline_context(cid)
    assert ctx is not None
    turn = build_turn_prefix(ctx, history)
    offturn = await build_offturn_prefix(cid, history, await get_settings())

    assert [m["role"] for m in turn] == ["system", "assistant", "user"]
    assert _wire(offturn) == _wire(turn)
    assert _wire(build_turn_prefix(ctx, history)) == _wire(turn)


async def test_group_offturn_prefix_matches_the_neutral_turn_prefix_under_a_custom_layout(client):
    layout_id, _ = await _active_layout(client)
    await _add_block(client, layout_id, content="Scene rules for {{cast}}.", region="prefix")
    await _add_block(client, layout_id, content="Earlier, {{char}} began.", region="prefix", role="user")
    cards = [
        (await client.post_json("/api/characters", json={"name": name, "description": text}))["id"]
        for name, text in (("Aria", "A tired ranger."), ("Kael", "A blunt smith."))
    ]
    cid = await client.create(
        "/api/conversations",
        json={
            "kind": "group",
            "title": "The Long Watch",
            "group_context_mode": "swap",
            "members": [{"character_card_id": card_id} for card_id in cards],
        },
    )
    members = await client.get_json(f"/api/conversations/{cid}/members")
    mid, _ = await add_message(cid, "user", "What was that noise?", 0)
    mid, _ = await add_message(
        cid, "assistant", "Aria lifts the lantern.", 1, parent_id=mid, speaker_member_id=members[0]["id"]
    )
    await set_active_leaf(cid, mid)

    history = await get_messages(cid)
    ctx = await load_pipeline_context(cid)
    assert ctx is not None
    turn = build_turn_prefix(ctx, history)
    offturn = await build_offturn_prefix(cid, history, await get_settings())

    assert [m["role"] for m in turn] == ["system", "user", "assistant"]
    assert _wire(offturn) == _wire(turn)


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


def test_a_section_block_renders_nothing_while_its_sections_are_empty():
    macros = Macros("U", "C", seed="s")
    layout = (*default_layout(), LayoutBlock(id="x", kind="custom", content="Where: {{scenario}}{{persona}}", sort_order=30))

    assert build_prefix("Sys", "", "", macros=macros, layout=layout) == build_prefix("Sys", "", "", macros=macros)
    filled = build_prefix("Sys", "", "Dunes {{char}}", macros=macros, layout=layout)[0]["content"]
    assert filled.endswith("\n\nWhere: Dunes C")


async def test_a_prefix_marker_with_a_user_role_becomes_a_pre_history_message(client):
    layout_id, seeded = await _active_layout(client)
    main = seeded["main_prompt"]["id"]
    await client.expect("patch", f"/api/prompt-blocks/{main}", json={"content": "Main text", "role": "user"})
    await client.expect(
        "patch", f"/api/prompt-blocks/{seeded['length_nudge']['id']}", json={"role": "user"}, expected_status=409
    )
    cid = "layout-role"
    await create_conversation(cid, "Role", "Iris", "A rainy archive.")
    mid, _ = await add_message(cid, "user", "Hello.", 0)
    await set_active_leaf(cid, mid)

    ctx = await load_pipeline_context(cid)
    assert ctx is not None
    turn = build_turn_prefix(ctx, await get_messages(cid))

    assert [m["role"] for m in turn] == ["system", "user"]
    assert turn[1]["content"] == "Main text\n\nHello."
    assert "Main text" not in turn[0]["content"]


async def test_a_section_block_below_the_history_moves_into_the_writer_request(client, llm_mock):
    layout_id, seeded = await _active_layout(client)
    scenario = seeded["Scenario"]["id"]
    order = await client.get_json(f"/api/prompt-layouts/{layout_id}/blocks")
    await client.put_checked(
        f"/api/prompt-layouts/{layout_id}/order",
        json={
            "prefix": [b["id"] for b in order if b["region"] == "prefix" and b["id"] != scenario],
            "writer": [scenario] + [b["id"] for b in order if b["region"] == "writer"],
        },
    )
    cid = await _card_conversation(client)

    llm_mock.enqueue_writer("Fine.")
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Hi", "attachments": []})).text
    writer = [call for call in llm_mock.captured if call["pass"] == "writer"][-1]["messages"]

    assert "Deep woods." not in writer[0]["content"]
    assert "Deep woods." in writer[-1]["content"]


async def test_description_and_personality_resolve_separately_solo_and_under_a_group_override(client):
    fields = "{{description}}|{{personality}}"
    card = await client.post_json("/api/characters", json={"name": "Aria", "description": "Ranger.", "personality": "Wary."})
    other = await client.post_json("/api/characters", json={"name": "Kael", "description": "Smith.", "personality": "Blunt."})
    solo, _ = persona_macros(await get_settings(), "Aria", None, card=card)
    assert solo.resolve_message(fields) == "Ranger.|Wary."

    cid = await client.create(
        "/api/conversations",
        json={
            "kind": "group",
            "title": "Watch",
            "members": [
                {"character_card_id": card["id"]},
                {"character_card_id": other["id"], "card_sheet_override": "Scene sheet."},
            ],
        },
    )
    cast = await resolve_cast(await get_conversation(cid))
    plain, overridden = (member_macros(Macros("U", "Watch"), member, "") for member in cast.members)
    assert plain is not None and overridden is not None
    assert plain.resolve_message(fields) == "Ranger.|Wary."
    assert overridden.resolve_message(fields) == "Scene sheet.|"


async def test_the_main_prompt_resolves_macros_and_keeps_its_bytes_without_them(client):
    layout_id, seeded = await _active_layout(client)
    main = seeded["main_prompt"]
    cid = "layout-main-macros"
    await create_conversation(cid, "Main", "Iris", "")
    mid, _ = await add_message(cid, "user", "Hello.", 0)
    await set_active_leaf(cid, mid)
    history = await get_messages(cid)

    ctx = await load_pipeline_context(cid)
    assert ctx is not None
    assert build_turn_prefix(ctx, history)[0]["content"].startswith(main["content"])
    await client.expect("patch", f"/api/prompt-blocks/{main['id']}", json={"content": "  You are {{char}}.\n"})
    ctx = await load_pipeline_context(cid)
    assert ctx is not None
    turn = build_turn_prefix(ctx, history)
    assert turn[0]["content"].startswith("  You are Iris.\n\n\n")
    assert _wire(await build_offturn_prefix(cid, history, await get_settings())) == _wire(turn)


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
    layout_id, seeded = await _active_layout(client)
    await _add_block(client, layout_id, content="Never summarized.", region="prefix", role="user")
    await client.expect("patch", f"/api/prompt-blocks/{seeded['Scenario']['id']}", json={"enabled": False})
    llm_mock.enqueue_writer("Custom take.")
    await client.post_checked(f"/api/conversations/{cid}/summarize", json=body)

    default_call, custom_call = (call["messages"] for call in llm_mock.captured[-2:])
    assert _wire(custom_call) == _wire(default_call)
