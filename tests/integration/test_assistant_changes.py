"""What the assistant proposes beyond one request's edits: a card that stays live across requests, deletes, Undo and the
user's own text, the lorebook drawer as part of the view, and secondary keywords."""

import json

import pytest

from backend.database import get_settings
from backend.features.assistant import ToolFailure
from backend.inference import LLMClient
from tests.integration.test_assistant import RUN, _apply, _body, _call, _propose, _reply, _tools, _use, build_library, frames


@pytest.fixture
async def library(client, db):
    return await build_library(client, db)


def _scripted(monkeypatch):
    """A model that replies from *script*, recording the messages of each call."""
    script, calls = [], []

    async def complete(self, **kwargs):
        calls.append(kwargs["messages"])
        yield script.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)
    return script, calls


async def test_a_pending_edit_from_an_earlier_request_stacks_and_one_out_of_date_starts_from_the_stored_text(
    client, library, db, monkeypatch
):
    script, calls = _scripted(monkeypatch)
    card = library["card"]
    script[:] = [
        _call("edit_character", {"field": "description", "mode": "find_replace", "find": "fence", "text": "fixer"}),
        _reply("Proposed."),
    ]
    events = frames(await client.post_checked(RUN, json=_body("Make her a fixer.", character_id=card)), resume=True)
    first = next(data["proposal"] for event, data in events if event == "confirm")
    assert first["fields"][0]["after"] == "A harbour fixer."
    assert events[-1][1].get("resume"), "a reply that proposed keeps its context for the next request"

    find = {"field": "description", "mode": "find_replace", "find": "harbour", "text": "dockside"}
    script[:] = [_call("edit_character", find), _reply("Proposed.")]
    calls.clear()
    events = frames(
        await client.post_checked(RUN, json={**_body("And dockside.", character_id=card), "pending": [first["proposal_id"]]})
    )
    # The request names what is still pending, with its pending text, which reads do not show.
    request = calls[0][-1]["content"]
    assert "still waiting for the user's Apply" in request and "its pending text:\nA harbour fixer." in request
    second = next(data["proposal"] for event, data in events if event == "confirm")
    assert second["replaces"] == first["proposal_id"]
    assert (second["fields"][0]["before"], second["fields"][0]["after"]) == ("A harbour fence.", "A dockside fixer.")
    stacked = json.loads(calls[1][-1]["content"])
    assert "on top of the edit" in stacked["note"]
    assert (await _apply(client, first["proposal_id"]))[0]["verdict"] == "expired", "the replaced one is forgotten"

    # The stored text moves under the pending one: the next edit starts from what is stored, and says why.
    await client.put_checked(f"/api/characters/{card}", json={"description": "A harbour fence, retired."})
    script[:] = [_call("edit_character", {**find, "text": "river"}), _reply("Proposed.")]
    calls.clear()
    events = frames(
        await client.post_checked(RUN, json={**_body("River, then.", character_id=card), "pending": [second["proposal_id"]]})
    )
    third = next(data["proposal"] for event, data in events if event == "confirm")
    assert (third["fields"][0]["before"], third["fields"][0]["after"]) == (
        "A harbour fence, retired.",
        "A river fence, retired.",
    )
    assert "has changed since" in json.loads(calls[1][-1]["content"])["note"]
    assert (await _apply(client, third["proposal_id"]))[0]["verdict"] == "applied"
    assert (await db.one("SELECT description FROM character_cards WHERE id = ?", (card,)))[0] == "A river fence, retired."

    # An id the browser names that is applied, unknown or forged tells the next request nothing.
    script[:] = [_reply("Nothing pending.")]
    calls.clear()
    frames(await client.post_checked(RUN, json={**_body("Anything left?"), "pending": [third["proposal_id"], "f" * 32]}))
    assert "still waiting" not in calls[0][-1]["content"]


async def test_each_delete_is_proposed_then_applied_through_the_apps_own_delete(client, library, db):
    persona = await client.create("/api/user-personas", json={"name": "Wren", "description": "A ferry pilot."})
    await client.put_checked("/api/settings", json={"active_persona_id": persona})
    tools = await _tools(mode="chat", conversation_id=library["chat"])
    proposals = {
        "persona": await _propose(tools, "delete_persona", persona_id=persona),
        "message": await _propose(tools, "delete_message", message_id=library["kept"]),
        "world": await _propose(tools, "delete_world", world_id=library["elsewhere"]),
        "document": await _propose(tools, "delete_document", document_id=library["document"]),
    }
    assert proposals["world"]["action"] == "delete" and "1 entry go with it" in proposals["world"]["fields"][0]["before"]
    assert "the active persona" in proposals["persona"]["fields"][0]["before"]
    # The app's own delete takes the message's other versions with it: the abandoned draft beside it.
    assert proposals["message"]["fields"][0]["before"].startswith("1 other message goes with it")
    # Proposing deleted nothing.
    assert (await db.one("SELECT COUNT(*) FROM worlds WHERE id = ?", (library["elsewhere"],)))[0] == 1
    with pytest.raises(ToolFailure, match="already proposed"):
        await _propose(tools, "delete_world", world_id=library["elsewhere"])

    verdicts = await _apply(client, *(proposal["proposal_id"] for proposal in proposals.values()))
    assert [verdict["verdict"] for verdict in verdicts] == ["applied"] * 4
    assert {"kind": "world", "world_id": library["elsewhere"], "deleted": True} in verdicts[2]["resources"]
    assert (await db.one("SELECT COUNT(*) FROM worlds WHERE id = ?", (library["elsewhere"],)))[0] == 0
    assert (await db.one("SELECT COUNT(*) FROM lorebook_entries WHERE world_id = ?", (library["elsewhere"],)))[0] == 0
    assert (await db.one("SELECT COUNT(*) FROM messages WHERE id = ?", (library["kept"],)))[0] == 0
    assert (await db.one("SELECT COUNT(*) FROM documents WHERE id = ?", (library["document"],)))[0] == 0
    # As the persona list's own delete does, the active persona no longer names it.
    assert (await get_settings()).get("active_persona_id") is None
    assert {"kind": "settings", "keys": ["active_persona_id"]} in verdicts[0]["resources"]
    # A second Apply is the first one's answer; nothing is deleted twice.
    assert (await _apply(client, proposals["world"]["proposal_id"]))[0]["repeated"] is True


async def test_a_character_delete_keeps_its_chats_unless_asked_and_a_chat_delete_waits_for_a_busy_chat(client, library, db):
    tools = await _tools(mode="library", character_id=library["other"])
    kept = await _propose(tools, "delete_character")
    assert "are kept" in kept["fields"][0]["before"]
    assert (await _apply(client, kept["proposal_id"]))[0]["verdict"] == "applied"
    assert (await db.one("SELECT COUNT(*) FROM conversations WHERE id = ?", (library["group"],)))[0] == 1

    tools = await _tools(mode="library", character_id=library["card"])
    gone = await _propose(tools, "delete_character", delete_chats=True)
    assert "are deleted with it" in gone["fields"][0]["before"]
    # Asking again with a different choice replaces the earlier proposal instead of being refused.
    assert (await _apply(client, gone["proposal_id"]))[0]["verdict"] == "applied"
    assert (await db.one("SELECT COUNT(*) FROM conversations WHERE id = ?", (library["chat"],)))[0] == 0

    chat = await client.create("/api/conversations", json={"character_card_id": None})
    tools = await _tools(mode="chat", conversation_id=chat)
    proposal = await _propose(tools, "delete_chat")
    from backend.api import deps

    deps._active_streams[chat] = object()
    try:
        busy = (await _apply(client, proposal["proposal_id"]))[0]
    finally:
        deps._active_streams.pop(chat, None)
    assert busy["verdict"] == "busy"
    assert (await _apply(client, proposal["proposal_id"]))[0]["verdict"] == "applied"
    assert (await db.one("SELECT COUNT(*) FROM conversations WHERE id = ?", (chat,)))[0] == 0


async def test_undo_puts_back_the_stored_text_and_the_users_own_text_is_what_apply_writes(client, library, db):
    persona = await client.create("/api/user-personas", json={"name": "Wren", "description": "A ferry pilot."})
    tools = await _tools(mode="library")
    edit = await _propose(tools, "edit_persona", persona_id=persona, field="description", mode="replace_all", text="A pilot.")
    pid = edit["proposal_id"]

    revised = await client.post_json("/api/assistant/revise", json={"proposal_id": pid, "text": "A night-ferry pilot."})
    assert revised["verdict"] == "revised" and revised["proposal"]["fields"][0]["after"] == "A night-ferry pilot."
    unchanged = await client.post_json("/api/assistant/revise", json={"proposal_id": pid, "text": "A ferry pilot."})
    assert unchanged["verdict"] == "refused"
    assert (await _apply(client, pid))[0]["verdict"] == "applied"
    stored = lambda: db.one("SELECT description FROM user_personas WHERE id = ?", (persona,))  # noqa: E731
    assert (await stored())[0] == "A night-ferry pilot."
    assert (await client.post_json("/api/assistant/revise", json={"proposal_id": pid, "text": "x"}))["verdict"] == "refused"

    [undone] = (await client.post_json("/api/assistant/undo", json={"proposal_ids": [pid]}))["results"]
    assert undone["verdict"] == "undone" and (await stored())[0] == "A ferry pilot."
    [again] = (await client.post_json("/api/assistant/undo", json={"proposal_ids": [pid]}))["results"]
    assert again["verdict"] == "refused", "an undone edit is pending again, not applied"
    # Pending again: it applies once more, and an Undo after the text moved on leaves it as it is.
    assert (await _apply(client, pid))[0]["verdict"] == "applied"
    await client.put_checked(f"/api/user-personas/{persona}", json={"description": "Someone else."})
    [stale] = (await client.post_json("/api/assistant/undo", json={"proposal_ids": [pid]}))["results"]
    assert stale["verdict"] == "stale" and (await stored())[0] == "Someone else."

    delete = await _propose(tools, "delete_persona", persona_id=persona)
    assert (await client.post_json("/api/assistant/revise", json={"proposal_id": delete["proposal_id"], "text": "x"}))[
        "verdict"
    ] == "refused"
    await _apply(client, delete["proposal_id"])
    [never] = (await client.post_json("/api/assistant/undo", json={"proposal_ids": [delete["proposal_id"]]}))["results"]
    assert never["verdict"] == "refused" and "cannot be undone" in never["message"]


async def test_the_lorebook_drawer_is_part_of_the_view_and_its_entry_is_the_default_target(client, library, db, monkeypatch):
    world = library["world"]
    entry = (await client.get_json(f"/api/worlds/{world}/entries"))[0]["id"]
    tools = await _tools(mode="chat", conversation_id=library["chat"], world_id=world, entry_id=entry)
    context = await tools["read_context"].run(tools["read_context"].arguments())
    assert (context.ids["world_id"], context.ids["entry_id"]) == (world, entry)
    listed, _ = await _use(tools, "list_lore_entries")
    assert [row["name"] for row in listed["rows"]] == ["Tide tax"], "the open world is the default"
    proposal = await _propose(tools, "edit_lore_entry", field="content", mode="replace_all", text="Paid at dawn.")
    assert proposal["target"] == {"kind": "lorebook_entry", "world_id": world, "entry_id": entry}

    # An entry named on its own is found in its own world; a world that is not its own is refused.
    inland = (await client.get_json(f"/api/worlds/{library['elsewhere']}/entries"))[0]["id"]
    setter = tools["set_lore_entry"]
    assert (await setter.run(setter.arguments(entry_id=inland, enabled="off"))).ids["world_id"] == library["elsewhere"]
    with pytest.raises(ToolFailure, match="no entry"):
        await setter.run(setter.arguments(world_id=world, entry_id=inland, enabled="on"))

    # The view note names both, and an entry outside the named world never reaches the model.
    script, calls = _scripted(monkeypatch)
    script[:] = [_reply("Seen.")]
    body = _body("Tighten this entry.", mode="chat", conversation_id=library["chat"], world_id=world, entry_id=entry)
    frames(await client.post_checked(RUN, json=body))
    assert f"(world_id {world})" in calls[0][-1]["content"] and f"(entry_id {entry})" in calls[0][-1]["content"]
    wrong = _body("x", world_id=world, entry_id=inland)
    assert (await client.post(RUN, json=wrong)).status_code == 404
    assert (await client.post(RUN, json=_body("x", entry_id=entry))).status_code == 422


async def test_secondary_keywords_are_set_with_selective_and_cleared_with_it(client, library):
    world = library["world"]
    entry = (await client.get_json(f"/api/worlds/{world}/entries"))[0]["id"]
    tools = await _tools(mode="library", world_id=world, entry_id=entry)
    setter = tools["set_lore_entry"]
    done = await setter.run(setter.arguments(secondary_keywords=["dusk", "dusk", "bell"]))
    assert sorted(done.data["changed"]) == ["secondary_keys", "selective"]
    stored = await client.get_json(f"/api/worlds/{world}/entries/{entry}")
    assert (stored["secondary_keys"], stored["selective"]) == (["dusk", "bell"], 1)
    assert (await _use(tools, "list_lore_entries"))[0]["rows"][0]["secondary_keywords"] == ["dusk", "bell"]
    await setter.run(setter.arguments(clear_secondary_keywords=True))
    stored = await client.get_json(f"/api/worlds/{world}/entries/{entry}")
    assert (stored["secondary_keys"], stored["selective"]) == ([], 0)

    create = tools["create_lore_entry"]
    made = await create.run(
        create.arguments(name="Bell", content="Rings at dusk.", keywords=["bell"], secondary_keywords=["dusk"])
    )
    created = await client.get_json(f"/api/worlds/{world}/entries/{made.ids['entry_id']}")
    assert (created["secondary_keys"], created["selective"]) == (["dusk"], 1)
