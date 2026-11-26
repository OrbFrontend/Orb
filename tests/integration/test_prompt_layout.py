"""Prompt layout: the prefix stays byte-stable around Writer-request edits, and every builder shares one layout."""

import json
from dataclasses import replace

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
from backend.prompting import (
    build_prefix,
    char_context,
    chat_instructions,
    conversation_sections,
    member_macros,
    persona_macros,
)
from backend.prompting.layout import SectionInputs, WriterInputs, render_writer_request, replay_reply
from backend.workflows.toolkit import build_offturn_prefix

_LONG_DRAFT = "word " * 60


def _wire(messages) -> str:
    return "\n".join(json.dumps(m, separators=(",", ":"), sort_keys=True) for m in messages)


async def _active_layout(client, name: str = "Custom") -> tuple[str, dict[str, dict]]:
    """A new active preset's id and its layout's seeded blocks, markers by kind and section blocks by label."""
    layout = await client.post_json("/api/presets", json={"name": name})
    await client.put_checked("/api/presets/active", json={"id": layout["id"]})
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


async def test_writer_blocks_send_their_roles_as_laid_out_and_the_editor_replays_them(client, llm_mock):
    await client.put_checked(
        "/api/settings",
        json={"enable_agent": True, "length_guard_enabled": True, "length_guard_max_words": 5},
    )
    layout_id, seeded = await _active_layout(client)
    system = await _add_block(client, layout_id, content="Injected sysprompt", role="system")
    ack = await _add_block(client, layout_id, content="Injected acknowledgement", role="assistant")
    praise = await _add_block(client, layout_id, content="Injected praise", role="user")
    order = await client.get_json(f"/api/prompt-layouts/{layout_id}/blocks")
    message = seeded["user_message"]["id"]
    ours = [system["id"], message, ack["id"], praise["id"]]
    await client.put_checked(
        f"/api/prompt-layouts/{layout_id}/order",
        json={
            "prefix": [b["id"] for b in order if b["region"] == "prefix"],
            "writer": [system["id"], message, ack["id"], praise["id"]]
            + [b["id"] for b in order if b["region"] == "writer" and b["id"] not in ours],
        },
    )
    cid = await _card_conversation(client)

    llm_mock.enqueue_writer(_LONG_DRAFT)
    llm_mock.enqueue_editor(None)
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Hi", "attachments": []})).text
    writer = [call for call in llm_mock.captured if call["pass"] == "writer"][-1]["messages"]
    editor = [call for call in llm_mock.captured if call["pass"] == "editor"][-1]["messages"]

    # Each block keeps its role after history, and nothing merges into the last history row.
    assert [m["role"] for m in writer[-5:]] == ["assistant", "system", "user", "assistant", "user"]
    assert writer[-4]["content"] == "Injected sysprompt"
    assert writer[-3]["content"].endswith("___\n\nHi")
    assert writer[-2]["content"] == "Injected acknowledgement"
    assert writer[-1]["content"] == "Injected praise\n\n"
    # The Editor replays the request verbatim, then the draft.
    assert editor[: len(writer)] == writer
    assert editor[len(writer)] == {"role": "assistant", "content": _LONG_DRAFT}


def test_anthropic_refuses_a_system_message_after_the_first_turn_rather_than_moving_it():
    import pytest

    from backend.inference.anthropic import build_request_body
    from backend.inference.errors import PromptShapeError

    messages = [
        {"role": "system", "content": "Main"},
        {"role": "user", "content": "Hi"},
        {"role": "system", "content": "Late"},
    ]
    with pytest.raises(PromptShapeError):
        build_request_body({"messages": messages}, "https://api.anthropic.com/v1", "claude")
    body = build_request_body({"messages": messages[:2]}, "https://api.anthropic.com/v1", "claude")
    assert body["system"] == "Main"


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
    prefix = build_prefix("Sys", SectionInputs(), history, Macros("U", "C", seed="s"), layout=layout)

    assert [m["role"] for m in prefix] == ["system", "assistant", "user"]
    assert prefix[1]["content"] == "One\n\nTwo\n\nGreeting"


def test_a_card_override_extends_the_main_prompt_through_original():
    layout = default_layout(main_prompt="Main")
    card = {"system_prompt": "{{Original}}\n\nStay terse. `{{original}}`"}

    assert char_context({}, card, layout).system_prompt == "Main\n\nStay terse. `{{original}}`"
    assert char_context({}, {"system_prompt": "Replace"}, layout).system_prompt == "Replace"
    assert char_context({"prevent_prompt_overrides": 1}, card, layout).system_prompt == "Main"
    # Post-history instructions have no global text in Orb: the macro leaves nothing, not a literal.
    assert chat_instructions({}, {"post_history_instructions": "{{original}}\nNo emojis."}) == "No emojis."
    assert chat_instructions({}, {"post_history_instructions": "{{original}}"}) == ""


def test_a_section_block_renders_nothing_while_its_sections_are_empty():
    macros = Macros("U", "C", seed="s")
    layout = (*default_layout(), LayoutBlock(id="x", kind="custom", content="Where: {{scenario}}{{persona}}", sort_order=30))

    empty = SectionInputs()
    assert build_prefix("Sys", empty, macros=macros, layout=layout) == build_prefix("Sys", empty, macros=macros)
    sections = conversation_sections({}, {"character_scenario": "Dunes {{char}}"}, char_context({}, None, layout), macros, "")
    filled = build_prefix("Sys", sections, macros=macros, layout=layout)[0]["content"]
    assert filled.endswith("\n\nWhere: Dunes C")


def test_a_backticked_section_placeholder_stays_literal():
    macros = Macros("U", "C", seed="s")
    layout = (
        *default_layout(),
        LayoutBlock(id="x", kind="custom", content="Write `{{scenario}}` for {{char}}.", sort_order=30),
    )

    # The literal neither takes the section's value nor gates the block on it.
    for scenario in ("", "Dunes"):
        prefix = build_prefix("Sys", SectionInputs(scenario=scenario), macros=macros, layout=layout)[0]["content"]
        assert prefix.endswith("\n\nWrite `{{scenario}}` for C.")


def test_a_section_name_means_its_section_in_every_layout_text():
    # A group speaker's macros carry their own card fields; layout text still reads the sections, empty in a group.
    speaker = Macros("U", "Mara", seed="s", description="Speaker card.", personality="Speaker mood.")
    sections = SectionInputs(scenario="Dunes", examples="<START>\nMara: hi\n<START>\nMara: bye\n")
    guidance = LayoutBlock(
        id="g",
        kind="scene_guidance",
        content="{{scene_guidance}}\nWhere: {{scenario}}. Who: {{description}}",
        role="user",
        region="writer",
    )
    request = render_writer_request((guidance,), WriterInputs(scene_guidance="Calm.", sections=sections), speaker)
    assert request.messages[0]["content"].startswith("Calm.\nWhere: Dunes. Who:")
    assert "Speaker card." not in request.messages[0]["content"]

    # The Main prompt fills them too, keeping its whitespace; outside a custom block the examples go in at once.
    main = LayoutBlock(id="m", kind="main_prompt", content="")
    prefix = build_prefix("  {{char}} in {{scenario}}:\n{{examples}}", sections, macros=speaker, layout=(main,))
    assert prefix[0]["content"] == "  Mara in Dunes:\nMara: hi\n\nMara: bye"


async def test_prefix_blocks_keep_their_place_and_role(client):
    layout_id, seeded = await _active_layout(client)
    main = seeded["main_prompt"]["id"]
    await client.expect("patch", f"/api/prompt-blocks/{main}", json={"content": "Main text", "role": "user"})
    late = await _add_block(client, layout_id, content="Late system", region="prefix", role="system")
    note = await _add_block(client, layout_id, content="Late note", region="prefix", role="user")
    order = await client.get_json(f"/api/prompt-layouts/{layout_id}/blocks")
    prefix = [b["id"] for b in order if b["region"] == "prefix" and b["id"] not in (late["id"], note["id"])]
    await client.put_checked(
        f"/api/prompt-layouts/{layout_id}/order",
        json={"prefix": [*prefix, note["id"], late["id"]], "writer": [b["id"] for b in order if b["region"] == "writer"]},
    )
    cid = "layout-role"
    await create_conversation(cid, "Role", "Iris", "A rainy archive.")
    mid, _ = await add_message(cid, "user", "Hello.", 0)
    await set_active_leaf(cid, mid)

    ctx = await load_pipeline_context(cid)
    assert ctx is not None
    turn = build_turn_prefix(ctx, await get_messages(cid))

    # Nothing is hoisted into a leading system message; only adjacent messages of one role merge.
    assert [m["role"] for m in turn] == ["user", "system", "user", "system", "user"]
    assert turn[0]["content"] == "Main text"
    assert turn[3]["content"] == "Late system"


def test_the_prefix_sends_no_empty_system_message():
    # An empty Main prompt still opens the prompt as system text, and every other block is user text.
    layout = tuple(block if block.kind == "main_prompt" else replace(block, role="user") for block in default_layout())
    history = [{"role": "user", "content": "Hi"}]

    prefix = build_prefix("", SectionInputs(scenario="Dunes"), history, Macros("U", "C", seed="s"), layout=layout)

    assert prefix == [{"role": "user", "content": "## Scenario\nDunes\n\nHi"}]


def test_a_trailing_assistant_block_is_the_reply_opening():
    layout = (
        *default_layout(),
        LayoutBlock(id="p", kind="custom", content="Sure:", role="assistant", region="writer", sort_order=30),
    )
    inputs = WriterInputs(user_message="Hi", speaker_open="## You are writing as A\n", speaker_close="Write as A only.")

    request = render_writer_request(layout, inputs, None)

    # The engine's closing line stays user text ahead of the opening, which keeps no trailing whitespace.
    assert [m["role"] for m in request.messages] == ["user", "assistant"]
    assert request.messages[0]["content"].endswith("Write as A only.")
    assert request.messages[1]["content"] == "Sure:"
    # Later passes replay the reply as the continuation of that opening, as the Writer produced it.
    assert replay_reply(request.messages, " Yes.")[-1] == {"role": "assistant", "content": "Sure: Yes."}


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


async def test_scene_guidance_and_user_message_render_from_their_own_text(client, llm_mock):
    await client.put_checked("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})
    layout_id, seeded = await _active_layout(client)
    scene, message = seeded["scene_guidance"]["id"], seeded["user_message"]["id"]
    # Text that drops its placeholder would silently drop the engine's part, so it is refused.
    await client.expect("patch", f"/api/prompt-blocks/{message}", json={"content": "___\n\nNo message"}, expected_status=409)
    # So is text naming it only where it never renders: inside a note, or as a backticked literal.
    for hidden in ("___\n\n{{// was: {{user_message}} }}", "___\n\n`{{user_message}}`"):
        await client.expect("patch", f"/api/prompt-blocks/{message}", json={"content": hidden}, expected_status=409)
    await client.expect(
        "patch", f"/api/prompt-blocks/{scene}", json={"content": "## Direction for {{char}}\n{{scene_guidance}}"}
    )
    await client.expect("patch", f"/api/prompt-blocks/{message}", json={"content": "___\n\n{{char}} hears:\n{{User_Message}}"})
    cid = await _card_conversation(client)

    llm_mock.enqueue_director([{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": ["tense"]}}}])
    llm_mock.enqueue_writer("Fine.")
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Knock.", "attachments": []})).text
    writer = [call for call in llm_mock.captured if call["pass"] == "writer"][-1]["messages"][-1]["content"]

    assert "## Direction for Aria\nWrite with tension" in writer
    assert "**Scene Guidance**" not in writer
    assert "___\n\nAria hears:\nKnock." in writer
    # The Inspector keeps the Director's record under its own heading.
    log = await client.get_json(f"/api/conversations/{cid}/messages/{(await get_messages(cid))[-1]['id']}/director-log")
    assert log["injection_block"].startswith("**Scene Guidance**\n\nWrite with tension")


async def test_the_engine_lines_lead_the_user_message_wherever_it_sits(client, llm_mock):
    await client.put_checked(
        "/api/settings",
        json={
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True},
            "length_guard_enabled": True,
            "length_guard_enforce": True,
        },
    )
    layout_id, seeded = await _active_layout(client)
    assert {"current_state", "length_nudge", "cue", "tool_notice"}.isdisjoint(seeded)
    order = await client.get_json(f"/api/prompt-layouts/{layout_id}/blocks")
    message = seeded["user_message"]["id"]
    await client.put_checked(
        f"/api/prompt-layouts/{layout_id}/order",
        json={
            "prefix": [b["id"] for b in order if b["region"] == "prefix"],
            "writer": [message] + [b["id"] for b in order if b["region"] == "writer" and b["id"] != message],
        },
    )
    cid = await _card_conversation(client)

    llm_mock.enqueue_director([{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": ["tense"]}}}])
    llm_mock.enqueue_writer("Fine.")
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Knock.", "attachments": []})).text
    writer = [call for call in llm_mock.captured if call["pass"] == "writer"][-1]["messages"][-1]["content"]

    assert writer.startswith("**Do not use tool or function calls this turn.**\n\n**Keep your response under ")
    assert writer.index("Knock.") < writer.index("**Scene Guidance**")


async def test_switched_off_markers_leave_the_prompt_but_the_engine_lines_and_the_inspector_stay(client, llm_mock):
    await client.put_checked(
        "/api/settings",
        json={
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True},
            "length_guard_enabled": True,
            "length_guard_enforce": True,
        },
    )
    _, seeded = await _active_layout(client)
    await client.expect("patch", f"/api/prompt-blocks/{seeded['main_prompt']['id']}", json={"content": "Main text"})
    for kind in ("main_prompt", "scene_guidance", "user_message"):
        await client.expect("patch", f"/api/prompt-blocks/{seeded[kind]['id']}", json={"enabled": False})
    card = await client.post_json(
        "/api/characters",
        json={"name": "Aria", "description": "A guard.", "first_mes": "Halt.", "system_prompt": "Card rules. {{original}}"},
    )
    cid = await client.create("/api/conversations", json={"character_card_id": card["id"]})

    llm_mock.enqueue_director([{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": ["tense"]}}}])
    llm_mock.enqueue_writer("Fine.")
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Knock.", "attachments": []})).text
    messages = [call for call in llm_mock.captured if call["pass"] == "writer"][-1]["messages"]
    wire = _wire(messages)

    assert "Main text" not in wire and "Card rules." not in wire
    assert "Knock." not in wire and "Write with tension" not in wire
    assert messages[-1]["content"].startswith("**Do not use tool or function calls this turn.**\n\n**Keep your response under ")
    log = await client.get_json(f"/api/conversations/{cid}/messages/{(await get_messages(cid))[-1]['id']}/director-log")
    assert log["injection_block"].startswith("**Scene Guidance**\n\nWrite with tension")
    breakdown = (await client.get_json(f"/api/conversations/{cid}/context-size"))["breakdown"]
    assert {"main_prompt", "scene_guidance", "user_message"}.isdisjoint(row["kind"] for row in breakdown)


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


async def test_the_summary_extends_the_turn_prefix(client, llm_mock):
    await client.put_checked(
        "/api/settings",
        json={"enable_agent": True, "length_guard_enabled": True, "length_guard_max_words": 5},
    )
    layout_id, seeded = await _active_layout(client)
    await _add_block(client, layout_id, content="House rules.", region="prefix", role="user")
    await client.expect("patch", f"/api/prompt-blocks/{seeded['Scenario']['id']}", json={"enabled": False})
    cid = await _card_conversation(client)
    llm_mock.enqueue_writer(_LONG_DRAFT)
    llm_mock.enqueue_editor(None)
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Hi", "attachments": []})).text
    writer = [call for call in llm_mock.captured if call["pass"] == "writer"][-1]

    llm_mock.enqueue_writer("The story so far.")
    _ = (await client.post_checked(f"/api/conversations/{cid}/summarize", json={"keep_count": 2})).text
    summary = llm_mock.captured[-1]

    # The summary is the turn's own prefix and history up to the kept messages, then only its request.
    shared = len(summary["messages"]) - 1
    assert _wire(summary["messages"][:shared]) == _wire(writer["messages"][:shared])
    assert _wire(summary.get("tools") or []) == _wire(writer.get("tools") or [])
    assert "Deep woods." not in summary["messages"][0]["content"]


async def test_every_call_sends_the_prefix_it_was_built_with(client, llm_mock):
    layout_id, seeded = await _active_layout(client)
    await client.expect(
        "patch",
        f"/api/prompt-blocks/{seeded['main_prompt']['id']}",
        json={"content": "About: {{description}}\nScene: {{scenario}}"},
    )
    card = await client.post_json(
        "/api/characters",
        json={
            "name": "Aria",
            "description": "She is calm. {{personality}}",
            "personality": "Moody",
            "scenario": "A ship.",
            "first_mes": "Hello.",
        },
    )
    cid = await client.create("/api/conversations", json={"character_card_id": card["id"]})
    offturn = await build_offturn_prefix(cid, await get_messages(cid), await get_settings())

    llm_mock.enqueue_writer("Fine.")
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Hi", "attachments": []})).text
    writer = [call for call in llm_mock.captured if call["pass"] == "writer"][-1]["messages"]

    # A field naming the other resolves in the first pass, the Main prompt fills the sections it names, and the Writer lane
    # sends the off-turn prefix's bytes.
    assert offturn[0]["content"].startswith("About: She is calm. Moody\nScene: A ship.\n\n")
    assert _wire(writer[: len(offturn)]) == _wire(offturn)
