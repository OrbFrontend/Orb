"""The assistant's remaining actions: creates, a chat's worlds, checkpoints, branch selection and handoffs to the chat."""

import pytest
from pydantic import ValidationError

from backend.api import deps
from backend.database import add_message, get_settings
from backend.features.assistant import ToolFailure
from backend.features.cards import expressions
from tests.integration.test_assistant import RUN, _body, _call, _reply, _script, _tools, build_library, frames


@pytest.fixture
async def library(client, db):
    return await build_library(client, db)


async def _run(client, monkeypatch, script, body):
    _script(monkeypatch, *script)
    return frames(await client.post_checked(RUN, json=body))


async def test_creates_take_authored_text_only_and_never_import_attach_or_activate(client, library, db, monkeypatch):
    async def no_fetch(*args, **kwargs):
        raise AssertionError("a create reached the URL-fetching import path")

    monkeypatch.setattr(expressions, "fetch_embedded_expressions", no_fetch)
    tools = await _tools(mode="chat", conversation_id=library["chat"])
    create = tools["create_character"]
    for bad in (
        {"extensions": {"chub": {"expressions": "https://example.invalid/pack.zip"}}},
        {"avatar_b64": "AAAA"},
        {"character_book": {"entries": []}},
        {"image_url": "https://example.invalid/a.png"},
        {"world_id": library["world"]},
        {"id": "chosen-id"},
        {"description": "x" * 4001},
        {"name": "  "},
        {"tags": "noir"},
    ):
        with pytest.raises(ValidationError):
            create.arguments.model_validate({"name": "Mara", **bad})
    first = await create.run(create.arguments(name="Mara", description="A second Mara.", tags=["Noir"]))
    second = await create.run(create.arguments(name="Mara"))
    ids = {first.committed[0]["character_id"], second.committed[0]["character_id"]}
    assert len(ids) == 2 and library["card"] not in ids, "a create never edits or reuses the card with that name"
    row = await db.one(
        "SELECT name, description, source_format, avatar_b64, world_id FROM character_cards WHERE id = ?",
        (first.ids["character_id"],),
    )
    assert tuple(row) == ("Mara", "A second Mara.", "manual", None, None)

    world = await tools["create_world"].run(tools["create_world"].arguments(name="Harbour"))
    world_id = world.committed[0]["world_id"]
    assert world_id != library["world"] and world.data["world"]["is_global"] is False
    effective = (await client.get_json(f"/api/conversations/{library['chat']}/worlds"))["world_ids"]
    assert world_id not in effective, "a new world is not attached to the chat in view"

    persona = await tools["create_persona"].run(tools["create_persona"].arguments(name="Wren", description="A pilot."))
    assert (await get_settings()).get("active_persona_id") != persona.ids["persona_id"]
    assert persona.committed == ({"kind": "persona", "persona_id": persona.ids["persona_id"]},)


async def test_set_chat_world_sets_an_explicit_state_keeps_other_choices_and_reports_a_global_world(client, library, db):
    chat = library["chat"]
    await client.put_checked(f"/api/conversations/{chat}/worlds/{library['elsewhere']}", json={"enabled": True})
    tools = await _tools(mode="chat", conversation_id=chat)
    set_world = tools["set_chat_world"]
    attached = await set_world.run(set_world.arguments(world_id=library["world"], attached=True))
    assert attached.data["attached"] is True and attached.data["changed"] is True
    assert attached.committed == ({"kind": "chat_worlds", "conversation_id": chat, "world_id": library["world"]},)
    again = await set_world.run(set_world.arguments(world_id=library["world"], attached=True))
    assert again.data["changed"] is False and again.committed == ()
    effective = (await client.get_json(f"/api/conversations/{chat}/worlds"))["world_ids"]
    assert set(effective) == {library["world"], library["elsewhere"]}, "the chat's other world choice is kept"

    # A global world: detaching it here turns it off for this chat alone, and the result says it is global.
    await client.put_checked(f"/api/worlds/{library['world']}", json={"is_global": True})
    detached = await set_world.run(set_world.arguments(world_id=library["world"], attached=False))
    assert (
        detached.data["attached"] is False and detached.data["world"]["is_global"] is True and "global" in detached.data["note"]
    )
    assert library["world"] in (await client.get_json(f"/api/conversations/{library['group']}/worlds"))["world_ids"]

    for bad in (
        {"world_id": "missing", "attached": True},
        {"world_id": library["world"], "attached": True, "conversation_id": "x"},
    ):
        with pytest.raises(ToolFailure):
            await set_world.run(set_world.arguments(**bad))
    with pytest.raises(ValidationError):
        set_world.arguments.model_validate({"world_id": library["world"], "attached": "toggle"})
    # In document mode a chat must be named.
    document = await _tools(mode="document", document_id=library["document"])
    with pytest.raises(ToolFailure, match="No chat is in view"):
        await document["set_chat_world"].run(document["set_chat_world"].arguments(world_id=library["world"], attached=True))


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("set_chat_world", lambda lib: {"world_id": lib["world"], "attached": True}),
        ("checkpoint_chat", lambda lib: {}),
        ("switch_branch", lambda lib: {"message_id": lib["kept"]}),
        ("set_chat", lambda lib: {"title": "Renamed"}),
    ],
)
async def test_a_chat_action_refuses_a_busy_or_deleting_chat_and_changes_nothing(client, library, db, name, arguments):
    chat = library["chat"]
    tool = (await _tools(mode="chat", conversation_id=chat))[name]
    before = await db.one("SELECT COUNT(*) FROM conversations")
    async with deps.conversation_stream_lock(chat):
        with pytest.raises(ToolFailure, match="generating"):
            await tool.run(tool.arguments(**arguments(library)))
    deps._deleting_resources.add(chat)
    try:
        with pytest.raises(ToolFailure, match="being deleted"):
            await tool.run(tool.arguments(**arguments(library)))
    finally:
        deps._deleting_resources.discard(chat)
    assert await db.one("SELECT COUNT(*) FROM conversations") == before
    assert library["world"] not in (await client.get_json(f"/api/conversations/{chat}/worlds"))["world_ids"]


async def test_switch_branch_selects_an_inactive_branch_of_its_own_chat_then_ends_the_run(client, library, db, monkeypatch):
    chat = library["chat"]
    abandoned = (await db.one("SELECT id FROM messages WHERE content = 'abandoned draft'"))[0]
    tools = await _tools(mode="chat", conversation_id=chat)
    switch = tools["switch_branch"]
    already = await switch.run(switch.arguments(message_id=library["kept"]))
    assert already.data["switched"] is False and already.committed == () and not already.ends_run
    with pytest.raises(ToolFailure):
        await switch.run(switch.arguments(conversation_id=library["group"], message_id=abandoned))

    events = await _run(
        client,
        monkeypatch,
        [_call("switch_branch", {"message_id": abandoned}), _reply("never")],
        _body("Go back to the abandoned draft.", mode="chat", conversation_id=chat),
    )
    assert dict(events)["mutation"]["resources"] == [{"kind": "branch", "conversation_id": chat}]
    assert [event for event, _ in events][-2:] == ["assistant_message", "done"] and events[-1][1]["reason"] == "reply"
    assert "request ends here" in events[-2][1]["text"]
    assert (await db.one("SELECT active_leaf_id FROM conversations WHERE id = ?", (chat,)))[0] == abandoned


async def test_a_checkpoint_copies_the_kept_branch_into_a_new_chat_and_leaves_the_source_as_it_was(client, library, db):
    chat = library["chat"]
    leaf = (await db.one("SELECT active_leaf_id FROM conversations WHERE id = ?", (chat,)))[0]
    tool = (await _tools(mode="chat", conversation_id=chat))["checkpoint_chat"]
    output = await tool.run(tool.arguments())
    copy = output.committed[0]["conversation_id"]
    assert copy != chat and output.data["checkpoint"]["title"].endswith("(checkpoint)")
    copied = [row["content"] for row in await client.get_json(f"/api/conversations/{copy}/messages")]
    source = [row["content"] for row in await client.get_json(f"/api/conversations/{chat}/messages")]
    assert copied == source and "abandoned draft" not in copied
    assert (await db.one("SELECT active_leaf_id FROM conversations WHERE id = ?", (chat,)))[0] == leaf


async def test_a_handoff_names_a_reply_on_the_kept_branch_and_ends_the_run_without_generating(client, library, db, monkeypatch):
    chat = library["chat"]
    count = (await db.one("SELECT COUNT(*) FROM messages"))[0]
    events = await _run(
        client,
        monkeypatch,
        [
            _call("rewrite_reply", {"message_id": library["kept"], "direction": "Make it colder."}),
            _reply("never"),
        ],
        _body("Rewrite the last reply colder.", mode="chat", conversation_id=chat),
    )
    assert dict(events)["handoff"] == {
        "call_id": "call1",
        "tool": "rewrite_reply",
        "handoff": {"conversation_id": chat, "message_id": library["kept"], "direction": "Make it colder."},
    }
    assert [event for event, _ in events][-2:] == ["handoff", "done"] and events[-1][1]["reason"] == "handoff"
    assert (await db.one("SELECT COUNT(*) FROM messages"))[0] == count, "the assistant generates nothing itself"

    tools = await _tools(mode="chat", conversation_id=chat)
    regenerate, rewrite = tools["regenerate_reply"], tools["rewrite_reply"]
    abandoned = (await db.one("SELECT id FROM messages WHERE content = 'abandoned draft'"))[0]
    user_line = (await db.one("SELECT id FROM messages WHERE conversation_id = ? AND role = 'user' LIMIT 1", (chat,)))[0]
    for message_id, problem in ((abandoned, "not showing"), (user_line, "user's own")):
        with pytest.raises(ToolFailure, match=problem):
            await regenerate.run(regenerate.arguments(message_id=message_id))
    with pytest.raises(ToolFailure):
        await regenerate.run(regenerate.arguments(conversation_id=library["group"], message_id=library["kept"]))
    with pytest.raises(ValidationError):
        rewrite.arguments.model_validate({"message_id": library["kept"], "direction": "   "})
    async with deps.conversation_stream_lock(chat):
        with pytest.raises(ToolFailure, match="generating"):
            await regenerate.run(regenerate.arguments(message_id=library["kept"]))
    # A chat still on screen while its delete runs is named as such, never as one that does not exist.
    deps._deleting_resources.add(chat)
    try:
        with pytest.raises(ToolFailure, match="being deleted, so nothing was done"):
            await regenerate.run(regenerate.arguments(message_id=library["kept"]))
    finally:
        deps._deleting_resources.discard(chat)
    with pytest.raises(ToolFailure, match="No chat has the id"):
        await regenerate.run(regenerate.arguments(conversation_id="no-such-chat", message_id=library["kept"]))
    # A group chat's own reply can be handed off by id alone: no speaker is inferred, and the chat keeps its speaker rules.
    group = library["group"]
    asked, _ = await add_message(group, "user", "Who keeps watch?", 0)
    reply, _ = await add_message(group, "assistant", "Ivo does.", 1, parent_id=asked, advance_leaf=True)
    group_tools = await _tools(mode="chat", conversation_id=group)
    output = await group_tools["regenerate_reply"].run(group_tools["regenerate_reply"].arguments(message_id=reply))
    assert output.handoff == {"conversation_id": group, "message_id": reply}
