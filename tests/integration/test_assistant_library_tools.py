"""The assistant's reads past the latest messages, its setters, chat starts and settings, its continue and open handoffs, and
whole-list card edits."""

import pytest
from pydantic import ValidationError

from backend.database import add_message, get_settings
from backend.features.assistant import ToolFailure
from tests.integration.test_assistant import MAX_MESSAGES, _apply, _propose, _tools, _use, build_library


@pytest.fixture
async def library(client, db):
    return await build_library(client, db)


async def test_messages_page_back_and_search_the_kept_branch_only(library):
    tools = await _tools(mode="chat", conversation_id=library["chat"])
    latest, _ = await _use(tools, "read_messages")
    assert latest["truncated"] is True and latest["earlier_from"] == latest["messages"][0]["id"]
    earlier, _ = await _use(tools, "read_messages", before_message_id=latest["earlier_from"])
    assert [row["content"] for row in earlier["messages"]] == [f"kept line {index}" for index in range(6)]
    assert earlier["truncated"] is False and earlier["earlier_from"] is None

    found, encoded = await _use(tools, "read_messages", query="LINE 1")
    assert found["matches"] == 1 + 10, "line 1 and lines 10 to 19, any case"
    assert "abandoned draft" not in encoded
    assert (await _use(tools, "read_messages", query="abandoned"))[0]["matches"] == 0, "a left branch is never searched"
    with pytest.raises(ToolFailure, match="not on the chat's kept branch"):
        await _use(tools, "read_messages", before_message_id=10**6)
    with pytest.raises(ToolFailure, match="leave before_message_id and query empty"):
        await _use(tools, "read_messages", message_id=library["kept"], query="x")
    assert len(latest["messages"]) == MAX_MESSAGES


async def test_entry_switches_set_a_state_and_active_lore_follows_what_the_chat_uses(client, library):
    chat, world = library["chat"], library["world"]
    await client.put_checked(f"/api/conversations/{chat}/worlds/{world}", json={"enabled": True})
    tools = await _tools(mode="chat", conversation_id=chat)
    active, _ = await _use(tools, "list_active_lore")
    assert [row["name"] for row in active["rows"]] == ["Tide tax"], "Inland is not in effect for this chat"
    assert [world["name"] for world in active["worlds_in_effect"]] == ["Harbour"]
    entry_id = active["rows"][0]["id"]

    setter = tools["set_lorebook_entry"]
    off = await setter.run(setter.arguments(world_id=world, entry_id=entry_id, enabled="off", keywords=["tax", "toll"]))
    assert off.data["changed"] == ["enabled", "keywords"]
    assert off.committed == ({"kind": "lorebook_entry", "world_id": world, "entry_id": entry_id},)
    stored = await client.get_json(f"/api/worlds/{world}/entries/{entry_id}")
    assert (stored["enabled"], stored["keywords"]) == (0, ["tax", "toll"])
    assert (await _use(tools, "list_active_lore"))[0]["rows"] == [], "a disabled entry is not in effect"
    again = await setter.run(setter.arguments(world_id=world, entry_id=entry_id, enabled="off"))
    assert again.committed == () and again.data["changed"] == []
    with pytest.raises(ToolFailure, match="nothing was asked"):
        await setter.run(setter.arguments(world_id=world, entry_id=entry_id))
    with pytest.raises(ToolFailure, match="no entry"):
        await setter.run(setter.arguments(world_id=library["elsewhere"], entry_id=entry_id, enabled="on"))


async def test_world_length_guard_and_phrase_bank_setters_write_only_what_differs(client, library):
    tools = await _tools(mode="library")
    world = tools["set_world"]
    made = await world.run(world.arguments(world_id=library["world"], is_global="on", name="Harbour"))
    assert made.data["changed"] == ["is_global"], "the name was already Harbour"
    assert (await client.get_json("/api/worlds"))[0]["is_global"] in (1, True)
    assert (await world.run(world.arguments(world_id=library["world"], is_global="on"))).committed == ()

    guard = tools["set_length_guard"]
    for bad in ({"max_words": 10}, {"max_words": 5000}, {"max_paragraphs": 21}, {"max_words": -1}):
        with pytest.raises(ValidationError):
            guard.arguments.model_validate(bad)
    before = await get_settings()
    set_words = await guard.run(guard.arguments(max_words=300))
    after = await get_settings()
    assert after["length_guard_max_words"] == 300
    assert after["length_guard_max_paragraphs"] == before["length_guard_max_paragraphs"]
    assert set_words.committed == ({"kind": "settings", "keys": ["length_guard_max_words"]},)
    assert after["length_guard_enabled"] == before["length_guard_enabled"], "the limits never switch the guard"

    phrase = tools["add_banned_phrase"]
    added = await phrase.run(phrase.arguments(phrase="a shiver ran down", variants=["shivers ran down"]))
    assert added.data["added"] is True
    bank = await client.get_json("/api/phrase-bank")
    assert [row["variants"] for row in bank if row["id"] == added.ids["group_id"]] == [
        ["a shiver ran down", "shivers ran down"]
    ]
    repeat = await phrase.run(phrase.arguments(phrase="A Shiver Ran Down"))
    assert repeat.data["added"] is False and repeat.committed == ()
    assert len(await client.get_json("/api/phrase-bank")) == len(bank)


async def test_a_started_chat_comes_from_the_route_writer_and_set_chat_writes_its_title_and_persona(client, library, db):
    await client.put_checked(f"/api/characters/{library['card']}", json={"first_mes": "Mara looks up."})
    tools = await _tools(mode="chat", conversation_id=library["chat"])
    started = await tools["start_chat"].run(tools["start_chat"].arguments(character_id=library["card"]))
    cid = started.ids["conversation_id"]
    assert cid != library["chat"] and started.data["conversation"]["title"] == "Mara"
    messages = await client.get_json(f"/api/conversations/{cid}/messages")
    assert [message["content"] for message in messages] == ["Mara looks up."], "the greeting is the first reply"
    with pytest.raises(ToolFailure, match="No character"):
        await tools["start_chat"].run(tools["start_chat"].arguments(character_id="missing"))

    persona = await client.create("/api/user-personas", json={"name": "Wren", "description": "A courier."})
    set_chat = tools["set_chat"]
    renamed = await set_chat.run(set_chat.arguments(conversation_id=cid, title="Quay", persona_lock_id=persona))
    assert renamed.data["changed"] == ["persona_lock_id", "title"]
    row = await db.one("SELECT title, persona_lock_id FROM conversations WHERE id = ?", (cid,))
    assert tuple(row) == ("Quay", persona)
    assert (await set_chat.run(set_chat.arguments(conversation_id=cid, title="Quay"))).committed == ()
    cleared = await set_chat.run(set_chat.arguments(conversation_id=cid, clear_persona_lock=True))
    assert cleared.data["persona_lock_id"] is None
    for bad, problem in (({"persona_lock_id": 999}, "No persona"), ({}, "nothing was asked")):
        with pytest.raises(ToolFailure, match=problem):
            await set_chat.run(set_chat.arguments(conversation_id=cid, **bad))


async def test_continue_hands_off_only_an_unanswered_message_or_a_named_group_member(client, library):
    chat = library["chat"]
    tools = await _tools(mode="chat", conversation_id=chat)
    cont = tools["continue_chat"]
    with pytest.raises(ToolFailure, match="already a reply"):
        await cont.run(cont.arguments())
    asked, _ = await add_message(chat, "user", "And then?", 99, parent_id=library["kept"], advance_leaf=True)
    handed = await cont.run(cont.arguments())
    assert handed.handoff == {"conversation_id": chat, "message_id": asked}
    with pytest.raises(ToolFailure, match="only for a group"):
        await cont.run(cont.arguments(speaker_member_id="someone"))

    group = await _tools(mode="chat", conversation_id=library["group"])
    cast = (await _use(group, "get_context"))[0]["chat"]["cast"]
    assert all(member.get("member_id") for member in cast)
    await add_message(library["group"], "assistant", "Mara speaks.", 0, advance_leaf=True)
    gcont = group["continue_chat"]
    with pytest.raises(ToolFailure, match="Name the member"):
        await gcont.run(gcont.arguments())
    turn = await gcont.run(gcont.arguments(speaker_member_id=cast[1]["member_id"]))
    assert turn.handoff["speaker_member_id"] == cast[1]["member_id"]
    by_name = await gcont.run(gcont.arguments(speaker_member_id=cast[1]["name"].upper()))
    assert by_name.handoff["speaker_member_id"] == cast[1]["member_id"], "a name resolves to the member id handed off"
    with pytest.raises(ToolFailure, match="no member"):
        await gcont.run(gcont.arguments(speaker_member_id="nobody"))

    opened = await tools["open_chat"].run(tools["open_chat"].arguments(conversation_id=library["group"]))
    assert opened.handoff["conversation_id"] == library["group"] and opened.committed == ()


async def test_list_fields_are_proposed_whole_and_apply_compares_the_stored_list(client, library, db):
    tools = await _tools(mode="library", character_id=library["card"])
    edit = tools["edit_character"]
    with pytest.raises(ToolFailure, match="is a list"):
        await edit.run(edit.arguments(field="tags", mode="find_replace", find="Noir", text="Gothic"))
    for wrong in ({"mode": "replace_all", "items": ["y"]}, {"mode": "add_items", "items": ["y"]}):
        with pytest.raises(ToolFailure, match="only for tags"):
            await edit.run(edit.arguments(field="description", text="x", **wrong))
    for mode, items in (("replace_all", ["Noir"]), ("add_items", ["Noir"])):
        with pytest.raises(ToolFailure, match="nothing to propose"):
            await edit.run(edit.arguments(field="tags", mode=mode, text="", items=items))
    with pytest.raises(ToolFailure, match="Not in tags"):
        await edit.run(edit.arguments(field="tags", mode="remove_items", text="", items=["Gothic"]))
    # Adding keeps what the card has; a second edit in the run builds on the first.
    preview = await _propose(tools, "edit_character", field="tags", mode="add_items", text="", items=["Harbour"])
    assert preview["fields"][0]["before"] == ["Noir"] and preview["fields"][0]["after"] == ["Noir", "Harbour"]
    stacked = await _propose(tools, "edit_character", field="tags", mode="add_items", text="", items=["Docks"])
    assert stacked["proposal_id"] == preview["proposal_id"] and stacked["fields"][0]["after"] == ["Noir", "Harbour", "Docks"]
    trimmed = await _propose(tools, "edit_character", field="tags", mode="remove_items", text="", items=["Docks"])
    assert trimmed["fields"][0]["after"] == ["Noir", "Harbour"]
    assert (await _apply(client, preview["proposal_id"]))[0]["verdict"] == "applied"
    assert (await client.get_json(f"/api/characters/{library['card']}"))["tags"] == ["Noir", "Harbour"]

    greetings = await _propose(
        tools, "edit_character", field="alternate_greetings", mode="replace_all", text="", items=["Hello.", "Evening."]
    )
    # Changed after the preview: Apply compares the list as stored, so it is stale.
    await client.put_checked(f"/api/characters/{library['card']}", json={"alternate_greetings": ["Changed."]})
    assert (await _apply(client, greetings["proposal_id"]))[0]["verdict"] == "stale"
    read, _ = await _use(tools, "get_character", fields=["alternate_greetings"])
    assert read["alternate_greetings"] == ["Changed."]
    tags, _ = await _use(tools, "get_character", fields=["tags"])
    assert tags["tags"] == ["Noir", "Harbour"] and "description" not in tags

    renamed = await _propose(tools, "edit_character", field="name", mode="replace_all", text="Mara Vell")
    assert (await _apply(client, renamed["proposal_id"]))[0]["verdict"] == "applied"
    with pytest.raises(ToolFailure, match="cannot be left empty"):
        await edit.run(edit.arguments(field="name", mode="replace_all", text=" "))
