"""The assistant's run and Stop routes, its view checks, and what each read tool is allowed to show."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from backend.api.assistant import apply_proposals, build_tools
from backend.api.routes.assistant import ASSISTANT_STREAM_KEY, resolve_view
from backend.api.schemas import AssistantViewContext
from backend.database import LIBRARY_VIEWS, add_message, close_wal_anchor, get_lorebook_entries, get_settings, open_wal_anchor
from backend.features.assistant import MAX_RESULT_CHARS, ToolFailure, run_assistant
from backend.features.assistant.contracts import encode_result
from backend.features.assistant.tools.reads import MAX_MESSAGES
from backend.features.assistant.tools.targets import CHARACTER_FIELDS
from backend.inference import LLMClient
from backend.workflows import prose_rewriter_host

SECRET = "sk-SECRET-KEY-12345"
RUN = "/api/assistant/run?operation_id=op-1"
_REAL_COMPLETE = LLMClient.complete


def frames(response, *, resume=False):
    """The stream's events; a reply's follow-up token, random per run, only when *resume* asks for it."""
    result = []
    for frame in response.text.split("\n\n"):
        if frame.startswith("event: "):
            event, data = frame.split("\ndata: ", 1)
            payload = json.loads(data) if data.startswith("{") else data
            if event == "event: done" and not resume:
                payload.pop("resume", None)
            result.append((event[7:], payload))
    return result


def _call(name, args):
    call = {"id": "call_0", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return {"type": "done", "message": {"content": "", "tool_calls": [call]}}


def _step(tool, message="Looking."):
    return _call("assistant_step", {"tool": tool, "message": message})


def _reply(message):
    return _call("reply", {"message": message})


def _body(text="What is here?", **context):
    return {"items": [{"kind": "user", "text": text}], "context": {"mode": "library", **context}}


@pytest.fixture
async def library(client, db):
    return await build_library(client, db)


async def build_library(client, db):
    """Two characters, a solo chat with a kept and an abandoned branch, a group, two worlds and a saved document."""
    await db.execute("UPDATE endpoints SET api_key = ?", (SECRET,))
    await db.commit()
    card = await client.create("/api/characters", json={"name": "Mara", "description": "A harbour fence.", "tags": ["Noir"]})
    other = await client.create("/api/characters", json={"name": "Ivo", "description": "A customs clerk."})
    await db.execute(
        "UPDATE character_cards SET avatar_b64 = 'AVATARBYTES', system_prompt = 'OVERRIDE', extensions = ? WHERE id = ?",
        (json.dumps({"orb": {"hidden": "EXTENSION"}}), card),
    )
    await db.commit()
    chat = await client.create("/api/conversations", json={"character_card_id": card})
    await db.execute("DELETE FROM messages WHERE conversation_id = ?", (chat,))
    await db.commit()
    parent = None
    for index in range(MAX_MESSAGES + 5):
        parent, _ = await add_message(
            chat, "user" if index % 2 == 0 else "assistant", f"kept line {index}", index, parent_id=parent
        )
    await add_message(chat, "assistant", "abandoned draft", MAX_MESSAGES + 4, parent_id=parent, advance_leaf=False)
    kept, _ = await add_message(chat, "assistant", "final kept line", MAX_MESSAGES + 5, parent_id=parent, advance_leaf=True)
    group = await client.post_json(
        "/api/conversations", json={"kind": "group", "members": [{"character_card_id": card}, {"character_card_id": other}]}
    )
    world = await client.create("/api/worlds", json={"name": "Harbour"})
    await client.post_json(
        f"/api/worlds/{world}/entries", json={"name": "Tide tax", "content": "Paid at dusk.", "keywords": ["tax"]}
    )
    elsewhere = await client.create("/api/worlds", json={"name": "Inland"})
    await client.post_json(f"/api/worlds/{elsewhere}/entries", json={"name": "Salt road", "content": "Closed.", "keywords": []})
    document = await client.create("/api/documents", json={"title": "Notes"})
    await client.put_checked(f"/api/documents/{document}", json={"content": "Saved text. " * 600, "expected_revision": 0})
    return {
        "card": card,
        "other": other,
        "chat": chat,
        "kept": kept,
        "group": group["id"],
        "world": world,
        "elsewhere": elsewhere,
        "document": document,
    }


async def _tools(**context):
    view = await resolve_view(AssistantViewContext(**context))
    return {tool.name: tool for tool in build_tools(await get_settings(), view)}


async def _use(tools, name, **arguments):
    output = await tools[name].run(tools[name].arguments(**arguments))
    encoded = encode_result(output.data)
    assert len(encoded) <= MAX_RESULT_CHARS, f"{name} returned {len(encoded)} characters"
    return output.data, encoded


async def test_a_run_streams_steps_and_results_then_one_terminal_event(client, library, monkeypatch):
    # An endpoint that sends no tool schemas (structured output, text completion) takes the two-call protocol.
    monkeypatch.setattr(LLMClient, "sends_tool_schemas", lambda self, messages, model, **kwargs: False)
    script = [_step("read_context"), _step("read_messages"), _call("read_messages", {"count": 2}), _step("reply", "")]
    # There the final answer is plain content after the step, never a string inside its arguments.
    script.append({"type": "done", "message": {"content": "Two lines."}})
    calls = []

    async def complete(self, **kwargs):
        calls.append(kwargs)
        yield script.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)
    body = _body(mode="chat", conversation_id=library["chat"])
    body["items"][:0] = [
        {"kind": "user", "text": "Fix the last line.", "view": "Chat: Mara"},
        {"kind": "tool", "tool": "edit_message", "text": "ok: approved and applied"},
    ]
    events = frames(await client.post_checked(RUN, json=body))
    names = [event for event, _ in events]
    assert names[0] == "start" and names[-1] == "done" and names.count("done") == 1 and "error" not in names
    assert [data for event, data in events if event == "tool_result"] == [
        {
            "call_id": "step1",
            "tool": "read_context",
            "ok": True,
            "summary": "Read the chat view.",
            "ids": {"character_id": library["card"], "conversation_id": library["chat"]},
        },
        {
            "call_id": "call2",
            "tool": "read_messages",
            "ok": True,
            "summary": f"Read the last 2 of {MAX_MESSAGES + 6} messages on the kept branch.",
            "ids": {"conversation_id": library["chat"], "message_ids": [library["kept"] - 2, library["kept"]]},
        },
    ]
    assert events[-2:] == [
        ("assistant_message", {"text": "Two lines.", "final": True}),
        ("done", {"reason": "reply", "message": ""}),
    ]
    assert calls[-1]["tool_choice"] == "none" and calls[-1]["messages"][-1]["role"] == "tool"
    # Assistant tools never join the turn's tool catalog, and replayed tool entries are not tool messages.
    assert [tool["function"]["name"] for tool in calls[0]["tools"]][0] == "assistant_step"
    # Every tool and reply, the two media tools last.
    enum = calls[0]["tools"][0]["function"]["parameters"]["properties"]["tool"]["enum"]
    assert len(enum) == 61 and enum[-3:] == ["generate_image", "generate_speech", "reply"]
    assert [message["role"] for message in calls[0]["messages"]] == ["system", "user", "assistant", "user"]
    assert SECRET not in json.dumps(calls[-1]["messages"])


async def test_an_endpoint_sending_native_tools_names_the_tool_and_its_arguments_in_one_call(client, library, monkeypatch):
    script = [_call("read_messages", {"count": 2}), _call("reply", {"message": "Two lines."})]
    calls = []

    async def complete(self, **kwargs):
        calls.append(kwargs)
        yield script.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)
    view = await resolve_view(AssistantViewContext(mode="chat", conversation_id=library["chat"]))
    tools = build_tools(await get_settings(), view)
    events = [
        event
        async for event in run_assistant(
            LLMClient("http://127.0.0.1:9/v1"),
            "m",
            settings=await get_settings(),
            tools=tools,
            history=[],
            request="What are the last two lines?",
            view=view,
        )
    ]
    results = [event for event in events if event["type"] == "tool_result"]
    assert [(result["call_id"], result["tool"], result["ok"]) for result in results] == [("call1", "read_messages", True)]
    assert events[-2:] == [
        {"type": "assistant_message", "text": "Two lines.", "final": True},
        {"type": "done", "reason": "reply", "message": ""},
    ]
    # Every call may pick any tool or reply, tools that take no arguments included, and no step tool exists.
    names = [tool["function"]["name"] for tool in calls[0]["tools"]]
    assert calls[0]["tool_choice"] == "required" and names[0] == "reply" and "read_context" in names
    assert "assistant_step" not in names and "assistant_step" not in calls[0]["messages"][0]["content"]
    # Each tool is described once, on the schema the call carries; the system prompt lists none.
    described = {tool["function"]["name"]: tool["function"]["description"] for tool in calls[0]["tools"]}
    system = calls[0]["messages"][0]["content"]
    assert all(tool.description in described[tool.name] and tool.description not in system for tool in tools)
    replayed = calls[1]["messages"][-2]["tool_calls"][0]["function"]
    assert replayed["name"] == "read_messages" and json.loads(replayed["arguments"]) == {"count": 2}


async def test_a_model_that_repeats_one_call_is_stopped_before_the_third_runs(client, library, monkeypatch):
    calls = []

    async def complete(self, **kwargs):
        calls.append(kwargs)
        yield _call("read_messages", {"count": 2})

    monkeypatch.setattr(LLMClient, "complete", complete)
    view = await resolve_view(AssistantViewContext(mode="chat", conversation_id=library["chat"]))
    events = [
        event
        async for event in run_assistant(
            LLMClient("http://127.0.0.1:9/v1"),
            "m",
            settings=await get_settings(),
            tools=build_tools(await get_settings(), view),
            history=[],
            request="What are the last two lines?",
            view=view,
        )
    ]
    assert len(calls) == 3 and len([event for event in events if event["type"] == "tool_result"]) == 2
    assert events[-1]["type"] == "done" and events[-1]["reason"] == "limit"


async def test_stop_names_its_operation_so_a_stale_stop_ends_nothing(client, monkeypatch):
    started = asyncio.Event()

    async def complete(self, **kwargs):
        started.set()
        await self.abort_token.wait()
        yield {"type": "done", "message": {}}

    monkeypatch.setattr(LLMClient, "complete", complete)
    run = asyncio.create_task(client.post(RUN, json=_body()))
    await asyncio.wait_for(started.wait(), 5)
    second = frames(await client.post("/api/assistant/run?operation_id=op-2", json=_body()))
    assert second == [("error", "Another generation is already running")]
    stale = await client.post_json("/api/assistant/stop?operation_id=op-0")
    assert stale == {"ok": True, "active": False, "settled": True} and not run.done()
    assert await client.post_json("/api/assistant/stop?operation_id=op-1") == {"ok": True, "active": True, "settled": True}
    events = frames(await asyncio.wait_for(run, 5))
    assert events[-1] == ("done", {"reason": "stopped", "message": ""})
    assert (await client.post("/api/assistant/stop")).status_code == 422
    assert ":" in ASSISTANT_STREAM_KEY  # a conversation id is a UUID and can never spell this key


@pytest.mark.parametrize(
    "change,status",
    [
        (lambda body, lib: body["context"].update(mode="chat", conversation_id="missing"), 404),
        (lambda body, lib: body["context"].update(mode="chat", conversation_id=lib["chat"], character_id=lib["other"]), 422),
        (
            lambda body, lib: body["context"].update(mode="document", document_id=lib["document"], conversation_id=lib["chat"]),
            422,
        ),
        (lambda body, lib: body["context"].update(mode="library", document_id=lib["document"]), 422),
        (lambda body, lib: body["context"].update(approved=True), 422),
        (lambda body, lib: body["items"].append({"kind": "assistant", "text": "last"}), 422),
        (lambda body, lib: body["items"][0].update(text="x" * 8001), 422),
        (lambda body, lib: body["items"].extend([{"kind": "user", "text": "more"}] * 100), 422),
        (lambda body, lib: body["items"][0].update(proposal_id="p1"), 422),
    ],
)
async def test_a_request_with_a_wrong_target_or_shape_never_reaches_the_model(client, library, monkeypatch, change, status):
    async def complete(self, **kwargs):
        raise AssertionError("the model was called")
        yield

    monkeypatch.setattr(LLMClient, "complete", complete)
    body = _body()
    change(body, library)
    assert (await client.post(RUN, json=body)).status_code == status


async def test_a_group_view_infers_no_character_and_asks_for_one(client, library):
    tools = await _tools(mode="chat", conversation_id=library["group"])
    with pytest.raises(ToolFailure) as refused:
        await _use(tools, "read_character")
    assert library["card"] in str(refused.value) and library["other"] in str(refused.value)
    data, _ = await _use(tools, "read_character", character_id=library["other"])
    assert data["name"] == "Ivo"
    context, _ = await _use(tools, "read_context")
    assert [member["name"] for member in context["cast"]] == ["Mara", "Ivo"]
    # A solo chat has one character, taken from the chat itself rather than from the browser.
    solo = await _tools(mode="chat", conversation_id=library["chat"])
    assert (await solo["read_character"].run(solo["read_character"].arguments())).ids["character_id"] == library["card"]
    assert "cast" not in (await _use(solo, "read_context"))[0], "the view note names a solo chat's character"


async def test_character_and_settings_projections_leave_out_everything_not_allowlisted(client, library):
    tools = await _tools(mode="library")
    card, encoded = await _use(tools, "read_character", character_id=library["card"])
    assert card["description"] == "A harbour fence." and card["tags"] == ["Noir"]
    for forbidden in ("AVATARBYTES", "OVERRIDE", "EXTENSION", "system_prompt", "extensions", "workflow_state"):
        assert forbidden not in encoded
    listed, encoded = await _use(tools, "list_characters", query="noir")
    assert [row["name"] for row in listed["rows"]] == ["Mara"] and "truncated" not in listed
    assert "AVATARBYTES" not in encoded and "display_scripts" not in encoded

    await client.put_checked("/api/settings", json={"enable_agent": False, "length_guard_enabled": True})
    summary, encoded = await _use(await _tools(mode="library"), "read_settings")
    assert SECRET not in encoded and "endpoint" not in encoded and "model" not in encoded
    assert summary["switches"]["agent"] is False
    assert summary["switches"]["length_guard"] == {"stored": True, "effective": False}
    assert summary["switches"]["director"] == {"stored": True, "effective": False}
    assert set(summary["samplers"]) == {"writer", "agent"} and "temperature" in summary["samplers"]["writer"]
    # The SQL views are allowlists too: no credential column, and nothing from the endpoints table.
    columns = {column for view in LIBRARY_VIEWS.values() for column in view}
    secretive = {"key", "token", "secret", "auth", "password", "pass"}
    assert not {column for column in columns if secretive & set(column.split("_"))}
    for view in LIBRARY_VIEWS:
        _, encoded = await _use(tools, "query_library", sql=f"SELECT * FROM {view} LIMIT 50")
        assert SECRET not in encoded


async def test_messages_follow_the_kept_branch_while_sql_sees_every_branch_and_no_secrets(client, library):
    tools = await _tools(mode="chat", conversation_id=library["chat"])
    data, encoded = await _use(tools, "read_messages")
    assert len(data["messages"]) == MAX_MESSAGES and data["truncated"] is True
    assert data["messages"][-1]["content"] == "final kept line" and "abandoned draft" not in encoded
    assert set(data["messages"][0]) == {"id", "number", "role", "content"}

    found, _ = await _use(
        tools, "query_library", sql="SELECT id, conversation_id FROM messages WHERE content = 'abandoned draft'"
    )
    assert found["rows"][0][1] == library["chat"]
    for sql in ("SELECT api_key FROM endpoints", "SELECT * FROM settings", "DELETE FROM messages"):
        with pytest.raises(ToolFailure) as refused:
            await _use(tools, "query_library", sql=sql)
        assert SECRET not in str(refused.value)

    # Outside a chat view a chat has to be named, and a name that matches nothing is an error, not another chat.
    document = await _tools(mode="document", document_id=library["document"])
    with pytest.raises(ToolFailure):
        await _use(document, "read_messages")
    with pytest.raises(ToolFailure):
        await _use(document, "read_messages", conversation_id="missing")
    assert (await _use(document, "read_messages", conversation_id=library["chat"], count=1))[0]["messages"][0]["id"] == library[
        "kept"
    ]


async def test_entries_and_documents_are_read_from_the_target_named(client, library):
    tools = await _tools(mode="library")
    entries, encoded = await _use(tools, "list_lore_entries", world_id=library["world"])
    assert [row["name"] for row in entries["rows"]] == ["Tide tax"] and "Salt road" not in encoded
    assert (await _use(tools, "list_lore_entries", world_id=library["world"], query="road"))[0]["rows"] == []
    with pytest.raises(ToolFailure):
        await _use(tools, "list_lore_entries", world_id="missing")
    assert {row["name"] for row in (await _use(tools, "list_worlds"))[0]["rows"]} >= {"Harbour", "Inland"}

    listed, _ = await _use(tools, "list_documents")
    assert [row["id"] for row in listed["rows"]] == [library["document"]] and "content" not in listed["rows"][0]
    page, _ = await _use(tools, "read_document", document_id=library["document"])
    assert page["truncated"] is True and page["total_chars"] == 7200 and "offset" not in page
    rest, _ = await _use(tools, "read_document", document_id=library["document"], offset=page["next_offset"])
    assert "truncated" not in rest and len(page["content"]) + len(rest["content"]) == 7200
    with pytest.raises(ToolFailure):
        await _use(tools, "read_document", document_id="missing")


def _script(monkeypatch, *replies):
    script = list(replies)

    async def complete(self, **kwargs):
        yield script.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)


async def test_a_failed_model_or_a_missing_endpoint_ends_the_stream_with_one_error(client, db, monkeypatch):
    unreadable = {"type": "done", "message": {"content": "I will look."}}
    _script(monkeypatch, unreadable, unreadable)
    events = frames(await client.post_checked(RUN, json=_body()))
    assert [event for event, _ in events] == ["start", "progress", "error"] and "after one retry" in events[-1][1]

    monkeypatch.setattr(LLMClient, "complete", _REAL_COMPLETE)
    await db.execute("UPDATE settings SET active_endpoint_id = NULL, agent_endpoint_id = NULL")
    await db.commit()
    events = frames(await client.post_checked(RUN, json=_body()))
    assert [event for event, _ in events] == ["start", "error"] and events[-1][1]["kind"] == "config"


async def test_long_text_is_measured_as_sent_so_every_result_stays_whole_json(client, library, db):
    # Quotes and newlines take two characters each once encoded: raw-length bounds used to overrun the cap mid-value.
    dialogue = '"Hold the line," she said.\n' * 400
    await db.execute(
        "UPDATE character_cards SET description = ?, personality = ?, scenario = ?, first_mes = ?, mes_example = ?, "
        "creator_notes = ? WHERE id = ?",
        (dialogue, dialogue, dialogue, dialogue, dialogue, "NOTES " + dialogue, library["card"]),
    )
    await db.commit()
    tools = await _tools(mode="library")
    card, encoded = await _use(tools, "read_character", character_id=library["card"])
    assert json.loads(encoded)["alternate_greeting_count"] == 0
    assert card["creator_notes"].startswith("NOTES") and all("…[+" in card[name] for name in CHARACTER_FIELDS)
    narrowed, _ = await _use(tools, "read_character", character_id=library["card"], fields=["creator_notes"])
    assert "tags" not in narrowed and len(narrowed["creator_notes"]) > len(card["creator_notes"])
    with pytest.raises(ValidationError):
        await _use(tools, "read_character", character_id=library["card"], fields=["avatar_b64"])

    await client.put_checked(f"/api/documents/{library['document']}", json={"content": dialogue * 2, "expected_revision": 1})
    page, encoded = await _use(tools, "read_document", document_id=library["document"])
    assert page["truncated"] is True and page["next_offset"] == len(page["content"])
    rest, _ = await _use(tools, "read_document", document_id=library["document"], offset=page["next_offset"])
    assert rest["offset"] == page["next_offset"] and rest["total_chars"] == len(dialogue * 2)

    with pytest.raises(ToolFailure) as refused:
        await _use(tools, "query_library", sql="SELECT " + ", ".join(["description"] * 20) + " FROM characters LIMIT 1")
    assert "substr" in str(refused.value)


async def test_adding_lore_commits_one_new_entry_per_call_and_reports_it_after_the_commit(client, library, monkeypatch):
    arguments = {"world_id": library["world"], "name": "Tide tax", "content": "Doubled in storms.", "keywords": ["storm"]}
    _script(
        monkeypatch,
        _call("create_lore_entry", arguments),
        _reply("Added."),
    )
    events = frames(await client.post_checked(RUN, json=_body("Add lore that the tide tax doubles in storms.")))
    entries = await get_lorebook_entries(library["world"])
    created = entries[-1]
    # Same name as an existing entry: a second entry, never an edit of the first.
    assert [entry["name"] for entry in entries] == ["Tide tax", "Tide tax"] and created["content"] == "Doubled in storms."
    start = next(data for event, data in events if event == "tool_start" and data["tool"] == "create_lore_entry")
    assert start == {"call_id": "call1", "tool": "create_lore_entry"}
    names = [event for event, _ in events]
    assert names.index("mutation") + 1 == names.index("tool_result") and names[-1] == "done"
    assert dict(events)["mutation"]["resources"] == [
        {"kind": "lorebook_entry", "world_id": library["world"], "entry_id": created["id"]}
    ]
    assert dict(events)["tool_result"]["ids"] == {"world_id": library["world"], "entry_id": created["id"]}

    tool = (await _tools(mode="library"))["create_lore_entry"]
    for bad in (
        {**arguments, "content": "x" * 4001},
        {**arguments, "name": "  "},
        {**arguments, "keywords": "storm"},
        {**arguments, "constant": "yes"},
        {**arguments, "extensions": {"x": 1}},
        {**arguments, "image_url": "http://example.invalid/a.png"},
    ):
        with pytest.raises(ValidationError):
            tool.arguments.model_validate(bad)
    with pytest.raises(ToolFailure):
        await tool.run(tool.arguments.model_validate({**arguments, "world_id": "missing"}))
    assert len(await get_lorebook_entries(library["world"])) == 2


async def test_a_change_runs_once_and_an_edit_is_proposed_once_per_request(client, library, db, monkeypatch):
    lore = {"world_id": library["world"], "name": "Ferry", "content": "Runs at dawn."}
    edit = {"message_id": library["kept"], "mode": "find_replace", "find": "kept", "text": "well kept"}
    _script(
        monkeypatch,
        _call("create_lore_entry", lore),
        _call("create_lore_entry", lore),
        _call("edit_message", edit),
        _call("edit_message", edit),
        _reply("Done."),
    )
    body = _body("Add the ferry lore and fix the line.", mode="chat", conversation_id=library["chat"])
    events = frames(await client.post_checked(RUN, json=body))
    results = [data for event, data in events if event == "tool_result"]
    assert [result["ok"] for result in results] == [True, False, True, False]
    assert "already made exactly this change once" in results[1]["summary"]
    assert "already proposed exactly this edit" in results[3]["summary"]
    assert [entry["name"] for entry in await get_lorebook_entries(library["world"])].count("Ferry") == 1
    [confirm] = [data["proposal"] for event, data in events if event == "confirm"]
    assert confirm["fields"][0]["after"] == "final well kept line", "applied once, not stacked onto itself"
    assert events[-1] == ("done", {"reason": "reply", "message": ""})


async def test_stop_during_a_lore_commit_waits_for_it_and_still_reports_its_id(client, library, monkeypatch):
    from backend.features.assistant.tools import actions

    writing, release = asyncio.Event(), asyncio.Event()
    real = actions.create_lorebook_entry

    async def slow_create(world_id, data):
        writing.set()
        await release.wait()
        return await real(world_id, data)

    monkeypatch.setattr(actions, "create_lorebook_entry", slow_create)
    arguments = {"world_id": library["world"], "name": "Ferry", "content": "Runs at dawn."}
    _script(monkeypatch, _call("create_lore_entry", arguments), _reply("x"))
    run = asyncio.create_task(client.post(RUN, json=_body("Add lore about the ferry.")))
    await asyncio.wait_for(writing.wait(), 5)
    stop = asyncio.create_task(client.post_json("/api/assistant/stop?operation_id=op-1"))
    await asyncio.sleep(0.05)
    assert not stop.done(), "Stop waits for the admitted write to settle"
    release.set()
    assert await asyncio.wait_for(stop, 5) == {"ok": True, "active": True, "settled": True}
    events = frames(await asyncio.wait_for(run, 5))
    created = (await get_lorebook_entries(library["world"]))[-1]
    assert created["name"] == "Ferry"
    assert dict(events)["mutation"]["resources"][0]["entry_id"] == created["id"]
    assert events[-1] == ("done", {"reason": "stopped", "message": 'Already done: Created lore entry "Ferry" in Harbour.'})


async def test_toggles_set_an_explicit_state_keep_other_keys_and_report_master_switches(client, monkeypatch):
    await client.put_checked(
        "/api/settings",
        json={
            "enable_agent": True,
            "length_guard_enabled": True,
            "enabled_tools": {"direct_scene": True, "editor_apply_patch": True},
        },
    )
    tools = {
        tool.name: tool for tool in build_tools(await get_settings(), await resolve_view(AssistantViewContext(mode="library")))
    }
    toggle = tools["set_pipeline_toggle"]
    # Another tab changes a different switch after this request read its settings.
    await client.put_checked("/api/settings", json={"enabled_tools": {"direct_scene": False, "editor_apply_patch": True}})

    output = await toggle.run(toggle.arguments.model_validate({"toggle": "editor", "enabled": False}))
    settings = await get_settings()
    assert settings["enabled_tools"]["editor_apply_patch"] is False and settings["length_guard_enabled"] == 0
    assert settings["enabled_tools"]["direct_scene"] is False, "the other tab's change survives"
    assert output.data["switches"] == {"output_auditor": False, "length_guard": False}
    assert set(output.data["changed"]) == {"output_auditor", "length_guard"}
    assert output.committed == ({"kind": "settings", "keys": ["enabled_tools", "length_guard_enabled"]},)
    again = await toggle.run(toggle.arguments.model_validate({"toggle": "editor", "enabled": False}))
    assert again.committed == () and "already off" in again.summary

    # On under a master switch that is off: stored on, reported as having no effect, and the master left alone.
    await client.put_checked("/api/settings", json={"enable_agent": False})
    output = await toggle.run(toggle.arguments.model_validate({"toggle": "director", "enabled": True}))
    assert output.data["switches"]["director"] == {"stored": True, "effective": False}
    assert "Agent" in output.data["note_gates"] and "no effect" in output.summary
    assert (await get_settings())["enable_agent"] == 0

    for bad in ({"toggle": "editor"}, {"toggle": "editor", "enabled": "off"}, {"toggle": "api_key", "enabled": False}):
        with pytest.raises(ValidationError):
            toggle.arguments.model_validate(bad)
    assert "flip" not in json.dumps(toggle.parameters) and "editor" in toggle.parameters["properties"]["toggle"]["enum"]

    summary = (await tools["read_settings"].run(tools["read_settings"].arguments())).data["switches"]
    assert summary["editor_audit_checks"]["banned_phrases"] is True and summary["editor_audit_checks_in_effect"] is False


async def test_a_workflow_toggle_runs_its_host_callback_like_its_route(client, monkeypatch):
    calls = []

    async def on_enabled(enabled):
        calls.append(enabled)

    monkeypatch.setattr(prose_rewriter_host, "on_enabled", on_enabled)
    toggle = {tool.name: tool for tool in await _tools_for_library()}["set_pipeline_toggle"]
    output = await toggle.run(toggle.arguments.model_validate({"toggle": "workflow:prose_rewriter", "enabled": False}))
    assert calls == [False] and (await get_settings())["workflow_enabled"]["prose_rewriter"] is False
    assert output.committed == ({"kind": "settings", "keys": ["workflow_enabled"]},)


async def _tools_for_library():
    return build_tools(await get_settings(), await resolve_view(AssistantViewContext(mode="library")))


APPLY = "/api/assistant/apply"


async def _apply(client, *proposal_ids):
    """The verdicts of one Apply, in order."""
    return (await client.post_json(APPLY, json={"proposal_ids": list(proposal_ids)}))["results"]


async def _propose(tools, name, **arguments):
    output = await tools[name].run(tools[name].arguments(**arguments))
    assert output.proposal is not None and output.committed == ()
    return output.proposal


async def test_a_proposal_writes_nothing_and_apply_commits_the_macro_resolved_preview_once(client, library, db, monkeypatch):
    kept = library["kept"]
    await db.execute("UPDATE messages SET writer_draft = 'pre-rewrite draft' WHERE id = ?", (kept,))
    await db.commit()
    arguments = {"message_id": kept, "mode": "find_replace", "find": "final", "text": "rolled {{roll::1d1000000}}"}
    _script(monkeypatch, _call("edit_message", arguments), _reply("x"))
    events = frames(await client.post_checked(RUN, json=_body("Fix that line.", mode="chat", conversation_id=library["chat"])))
    # The run carries on past a proposal, to its own reply.
    assert [event for event, _ in events][-4:] == ["confirm", "progress", "assistant_message", "done"]
    assert events[-1][1]["reason"] == "reply" and "mutation" not in dict(events)
    preview = dict(events)["confirm"]["proposal"]
    after = preview["fields"][0]["after"]
    # The macro rolled once, for the preview; nothing was written yet.
    assert preview["fields"][0]["before"] == "final kept line" and after.startswith("rolled ") and "{{" not in after
    assert (await db.one("SELECT content FROM messages WHERE id = ?", (kept,)))[0] == "final kept line"
    assert preview["target"] == {"kind": "message", "conversation_id": library["chat"], "message_id": kept}

    [applied] = await _apply(client, preview["proposal_id"])
    assert applied["verdict"] == "applied" and applied["resources"] == [preview["target"]]
    row = await db.one("SELECT content, writer_draft FROM messages WHERE id = ?", (kept,))
    assert tuple(row) == (after, None), "the previewed text is what commits, and the edit route's draft clearing runs"

    # Another edit afterwards; a repeated Apply answers from the first one and writes nothing.
    await client.post_checked(f"/api/conversations/{library['chat']}/messages/{kept}/edit", json={"content": "by hand"})
    [again] = await _apply(client, preview["proposal_id"])
    assert again["repeated"] is True and again["resources"] == applied["resources"]
    assert (await db.one("SELECT content FROM messages WHERE id = ?", (kept,)))[0] == "by hand"
    # Apply takes proposal ids alone: arguments or after values cannot be substituted.
    forged = {"proposal_ids": [preview["proposal_id"]], "after": "forged"}
    assert (await client.post(APPLY, json=forged)).status_code == 422


async def test_an_edit_after_the_preview_is_a_409_and_concurrent_applies_commit_once(client, library, db, monkeypatch):
    tools = await _tools(mode="library", character_id=library["card"])
    preview = await _propose(tools, "edit_character", field="description", mode="replace_all", text="A retired fence.")
    await client.put_checked(f"/api/characters/{library['card']}", json={"description": "Edited in the library."})
    [stale] = await _apply(client, preview["proposal_id"])
    assert stale["verdict"] == "stale" and stale["fields"] == ["description"]
    assert (await db.one("SELECT description FROM character_cards WHERE id = ?", (library["card"],)))[0] == (
        "Edited in the library."
    )

    # Two Applies at once: the second arrives while the first is inside its write, waits for it, and answers with its result.
    from backend.features.assistant.tools import proposals

    preview = await _propose(tools, "edit_character", field="scenario", mode="replace_all", text="Dawn at the docks.")
    writing, release = asyncio.Event(), asyncio.Event()
    real, writes = proposals.update_character_card, []

    async def held(*args, **kwargs):
        writes.append(args)
        writing.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(proposals, "update_character_card", held)
    first = asyncio.create_task(_apply(client, preview["proposal_id"]))
    await asyncio.wait_for(writing.wait(), 5)
    second = asyncio.create_task(_apply(client, preview["proposal_id"]))
    await asyncio.sleep(0.05)
    assert not second.done() and proposals.get_proposal(preview["proposal_id"]).lock.locked()
    release.set()
    [first], [second] = await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert first["verdict"] == second["verdict"] == "applied" and len(writes) == 1
    assert first.get("repeated") is None and second["repeated"] is True
    conv = await db.one("SELECT character_scenario FROM conversations WHERE id = ?", (library["chat"],))
    assert conv[0] == "Dawn at the docks."


@pytest.mark.parametrize("target", ["lorebook_entry", "persona", "character", "message"])
async def test_an_edit_racing_the_apply_waits_for_it_and_is_not_overwritten(client, library, db, monkeypatch, target):
    from backend.database.queries import character_cards, messages, user_personas, worlds

    if target == "lorebook_entry":
        entry = (await get_lorebook_entries(library["world"]))[0]
        tools = await _tools(mode="library")
        arguments = {"world_id": library["world"], "entry_id": entry["id"], "field": "content"}
        preview = await _propose(tools, "edit_lore_entry", **arguments, mode="find_replace", find="dusk", text="dawn")
        module, url = worlds, f"/api/worlds/{library['world']}/entries/{entry['id']}"
        race, read = ("put", {"content": "Abolished."}), ("SELECT content FROM lorebook_entries WHERE id = ?", entry["id"])
    elif target == "persona":
        persona = await client.create("/api/user-personas", json={"name": "Wren", "description": "A ferry pilot."})
        tools = await _tools(mode="library")
        preview = await _propose(tools, "edit_persona", persona_id=persona, field="description", mode="replace_all", text="X")
        module, url = user_personas, f"/api/user-personas/{persona}"
        race, read = ("put", {"description": "Abolished."}), ("SELECT description FROM user_personas WHERE id = ?", persona)
    elif target == "character":
        tools = await _tools(mode="library", character_id=library["card"])
        preview = await _propose(tools, "edit_character", field="description", mode="replace_all", text="X")
        module, url = character_cards, f"/api/characters/{library['card']}"
        race = ("put", {"description": "Abolished."})
        read = ("SELECT description FROM character_cards WHERE id = ?", library["card"])
    else:
        tools = await _tools(mode="chat", conversation_id=library["chat"])
        preview = await _propose(tools, "edit_message", message_id=library["kept"], mode="replace_all", text="X")
        module, url = messages, f"/api/conversations/{library['chat']}/messages/{library['kept']}/edit"
        race, read = ("post", {"content": "Abolished."}), ("SELECT content FROM messages WHERE id = ?", library["kept"])
    real = module.check_expected
    racer: dict[str, asyncio.Task] = {}

    async def compared_then_raced(*args, **kwargs):
        held = await real(*args, **kwargs)
        # The comparison passed and this transaction holds the write lock; an ordinary edit starting now must wait for it.
        racer["task"] = asyncio.create_task(getattr(client, race[0])(url, json=race[1]))
        await asyncio.sleep(0.3)
        assert not racer["task"].done(), "the racing edit could not land between the comparison and the write"
        return held

    revision = (await db.one("SELECT content_revision FROM worlds WHERE id = ?", (library["world"],)))[0]
    monkeypatch.setattr(module, "check_expected", compared_then_raced)
    assert (await _apply(client, preview["proposal_id"]))[0]["verdict"] == "applied"
    assert (await racer["task"]).status_code == 200
    # The racing edit landed after the Apply, so it is what remains; a World's revision advanced once for each edit.
    assert (await db.one(read[0], (read[1],)))[0] == "Abolished."
    bumps = 2 if target == "lorebook_entry" else 0
    assert (await db.one("SELECT content_revision FROM worlds WHERE id = ?", (library["world"],)))[0] == revision + bumps


async def test_proposals_check_their_text_and_an_unknown_or_expired_one_is_inert(client, library, db, monkeypatch):
    from backend.features.assistant.tools import proposals

    tools = await _tools(mode="library", character_id=library["card"])
    edit = tools["edit_character"]
    for arguments, problem in (
        ({"mode": "find_replace", "find": "nowhere", "text": "x"}, "does not occur"),
        ({"mode": "find_replace", "find": "", "text": "x"}, "was empty"),
        ({"mode": "replace_all", "text": "A harbour fence."}, "nothing to propose"),
    ):
        with pytest.raises(ToolFailure, match=problem):
            await edit.run(edit.arguments(field="description", **arguments))
    await client.put_checked(f"/api/characters/{library['card']}", json={"description": "aa then aa"})
    with pytest.raises(ToolFailure, match="more than once"):
        await edit.run(edit.arguments(field="description", mode="find_replace", find="aa", text="b"))
    every = (await edit.run(edit.arguments(field="description", mode="replace_every", find="aa", text="b"))).proposal
    assert every is not None and every["fields"][0]["after"] == "b then b"
    # A find copied from a result as its JSON shows it matches the text it was copied from.
    await client.put_checked(f"/api/characters/{library['card']}", json={"personality": 'Dry.\n\nSays "no" often.'})
    copied = {"mode": "find_replace", "find": '\\n\\nSays \\"no\\" often.', "text": ""}
    escaped = (await edit.run(edit.arguments(field="personality", **copied))).proposal
    assert escaped is not None and escaped["fields"][0]["after"] == "Dry."
    # A list edit carries no text.
    tagged = (await edit.run(edit.arguments(field="tags", mode="add_items", items=["Harbour"]))).proposal
    assert tagged is not None and "Harbour" in tagged["fields"][0]["after"]
    for bad in ({"text": "x" * 8001}, {"field": "system_prompt"}, {"field": "creator"}, {"mode": "append"}):
        with pytest.raises(ValidationError):
            edit.arguments.model_validate({"field": "description", "mode": "replace_all", "text": "x", **bad})

    assert (await _apply(client, "0" * 32))[0]["verdict"] == "expired"
    preview = await _propose(tools, "edit_character", field="description", mode="replace_all", text="Expired edit.")
    now = proposals._clock()
    monkeypatch.setattr(proposals, "_clock", lambda: now + proposals.PROPOSAL_TTL_SECS + 1)
    assert (await _apply(client, preview["proposal_id"]))[0]["verdict"] == "expired"
    assert (await db.one("SELECT description FROM character_cards WHERE id = ?", (library["card"],)))[0] == "aa then aa"


async def test_a_chat_edit_refuses_a_busy_or_deleted_chat_and_a_cancelled_apply_still_settles(client, library, db, monkeypatch):
    from backend.api import deps
    from backend.features.assistant.tools import proposals

    tools = await _tools(mode="chat", conversation_id=library["chat"])
    kept = library["kept"]
    preview = await _propose(tools, "edit_message", message_id=kept, mode="replace_all", text="Settled line.")
    # A chat that is generating holds its lane: Apply is refused, not queued, and can be tried again afterwards.
    async with deps.conversation_stream_lock(library["chat"]):
        [busy] = await _apply(client, preview["proposal_id"])
        with pytest.raises(ToolFailure, match="generating"):
            await tools["edit_message"].run(tools["edit_message"].arguments(message_id=kept, mode="replace_all", text="y"))
    assert busy["verdict"] == "busy"

    # Cancelling the request mid-commit leaves the commit to finish, and a repeated Apply reports it.
    writing, release = asyncio.Event(), asyncio.Event()
    real = proposals.save_message_edit

    async def slow(*args, **kwargs):
        writing.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(proposals, "save_message_edit", slow)
    task = asyncio.create_task(apply_proposals([preview["proposal_id"]]))
    await asyncio.wait_for(writing.wait(), 5)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert (await db.one("SELECT content FROM messages WHERE id = ?", (kept,)))[0] == "Settled line."
    assert (await _apply(client, preview["proposal_id"]))[0]["repeated"] is True

    # A pending proposal whose chat is deleted finds nothing to edit.
    preview = await _propose(
        await _tools(mode="chat", conversation_id=library["chat"]),
        "edit_message",
        message_id=kept,
        mode="replace_all",
        text="Never saved.",
    )
    await client.delete_checked(f"/api/conversations/{library['chat']}")
    assert (await _apply(client, preview["proposal_id"]))[0]["verdict"] == "missing"


async def test_a_tools_panel_save_after_the_assistants_toggle_keeps_it(client):
    await client.put_checked(
        "/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": False, "editor_apply_patch": True}}
    )
    toggle = {tool.name: tool for tool in await _tools_for_library()}["set_pipeline_toggle"]
    output = await toggle.run(toggle.arguments.model_validate({"toggle": "editor", "enabled": True}))
    # On names the Length Guard too, and says it stays off.
    assert output.data["switches"]["length_guard"] is False
    assert "Length Guard stays off" in output.summary
    await toggle.run(toggle.arguments.model_validate({"toggle": "output_auditor", "enabled": False}))
    # The Tools panel, unaware of that, saves the one flag its checkbox changed.
    await client.put_checked("/api/settings", json={"enabled_tools": {"direct_scene": True}})
    tools = (await get_settings())["enabled_tools"]
    assert tools["direct_scene"] is True and tools["editor_apply_patch"] is False


async def test_a_failure_after_the_commit_is_finished_by_the_retry_never_reported_as_stale(client, library, db, monkeypatch):
    from backend.database.queries import messages
    from backend.features.assistant.tools import proposals

    # A card: its write commits, then carrying it to the card's chats fails. A repeated Apply returns the first answer and runs
    # that step again; nothing is stale.
    tools = await _tools(mode="library", character_id=library["card"])
    preview = await _propose(tools, "edit_character", field="scenario", mode="replace_all", text="A retired fence.")
    real_sync, fails = proposals.sync_conversations_for_card, [RuntimeError("database is locked")]

    async def flaky_sync(*args, **kwargs):
        if fails:
            raise fails.pop()
        return await real_sync(*args, **kwargs)

    monkeypatch.setattr(proposals, "sync_conversations_for_card", flaky_sync)
    with pytest.raises(RuntimeError):
        await apply_proposals([preview["proposal_id"]])
    [retry] = await _apply(client, preview["proposal_id"])
    assert retry["verdict"] == "applied" and retry["repeated"] is True
    conv = await db.one("SELECT character_scenario FROM conversations WHERE id = ?", (library["chat"],))
    assert conv[0] == "A retired fence.", "the follow-up the first Apply missed ran on the retry"

    # A message: the edit and its invariants are one transaction, so a failure in any of them leaves nothing half done.
    kept = library["kept"]
    await db.execute("UPDATE messages SET writer_draft = 'pre-rewrite draft' WHERE id = ?", (kept,))
    await db.commit()
    chat_tools = await _tools(mode="chat", conversation_id=library["chat"])
    preview = await _propose(chat_tools, "edit_message", message_id=kept, mode="replace_all", text="Edited once.")
    real_stale, fails = messages.mark_changesets_stale_on, [RuntimeError("database is locked")]

    async def flaky_stale(*args, **kwargs):
        if fails:
            raise fails.pop()
        return await real_stale(*args, **kwargs)

    monkeypatch.setattr(messages, "mark_changesets_stale_on", flaky_stale)
    with pytest.raises(RuntimeError):
        await apply_proposals([preview["proposal_id"]])
    row = await db.one("SELECT content, writer_draft FROM messages WHERE id = ?", (kept,))
    assert tuple(row) == ("final kept line", "pre-rewrite draft"), "rolled back whole"
    assert (await _apply(client, preview["proposal_id"]))[0]["verdict"] == "applied"
    row = await db.one("SELECT content, writer_draft FROM messages WHERE id = ?", (kept,))
    assert tuple(row) == ("Edited once.", None)


async def test_a_clipped_read_never_becomes_a_whole_field_rewrite_and_the_whole_field_can_be_read(client, library, db):
    personality = "Opening paragraph. " + "Middle detail. " * 150 + "The ending."
    description = "Start. " + "Long history. " * 600 + "Final clause."
    await client.put_checked(
        f"/api/characters/{library['card']}", json={"personality": personality, "description": description}
    )
    tools = await _tools(mode="library", character_id=library["card"])
    seen, _ = await _use(tools, "read_character")
    assert "…[+" in seen["personality"] and "text_offset" in seen["note"]
    edit = tools["edit_character"]
    with pytest.raises(ToolFailure, match="not part of the field"):
        await edit.run(edit.arguments(field="personality", mode="replace_all", text=seen["personality"]))
    # Named alone, a field shorter than one page reads whole, unclipped.
    whole, _ = await _use(tools, "read_character", fields=["personality"])
    assert whole["personality"] == personality and "next_offset" not in whole
    # A field longer than an argument may carry cannot be rewritten whole; it reads in pages, and find_replace reaches its tail.
    with pytest.raises(ToolFailure, match="find_replace") as refused:
        await edit.run(edit.arguments(field="description", mode="replace_all", text="Short."))
    assert "text_offset" in str(refused.value)
    pages, offset = [], 0
    while offset is not None:
        page, _ = await _use(tools, "read_character", fields=["description"], text_offset=offset)
        pages.append(page["description"])
        offset = page.get("next_offset")
    assert "".join(pages) == description and len(pages) > 1
    preview = await _propose(
        tools, "edit_character", field="description", mode="find_replace", find="Final clause.", text="End."
    )
    assert preview["fields"][0]["after"] == description.replace("Final clause.", "End.")

    # A long reply reads whole by its id, from any branch of its chat only.
    reply = "Line of a long reply. " * 140
    await db.execute("UPDATE messages SET content = ? WHERE id = ?", (reply, library["kept"]))
    await db.commit()
    chat = await _tools(mode="chat", conversation_id=library["chat"])
    latest, _ = await _use(chat, "read_messages", count=1)
    assert latest["messages"][0]["content"].endswith("chars]") and "message_id" in latest["note"]
    one, _ = await _use(chat, "read_messages", message_id=library["kept"])
    assert one["content"] == reply and one["number"] and "on_kept_branch" not in one
    with pytest.raises(ToolFailure):
        await _use(await _tools(mode="chat", conversation_id=library["group"]), "read_messages", message_id=library["kept"])


async def test_an_apply_during_a_chat_delete_is_retryable_and_a_rerolling_greeting_is_refused_up_front(client, library):
    from backend.api import deps

    tools = await _tools(mode="chat", conversation_id=library["chat"])
    preview = await _propose(tools, "edit_message", message_id=library["kept"], mode="replace_all", text="Kept.")
    deps._deleting_resources.add(library["chat"])
    try:
        [deleting] = await _apply(client, preview["proposal_id"])
    finally:
        deps._deleting_resources.discard(library["chat"])
    assert deleting["verdict"] == "busy"
    # The delete failed and the chat lives on: nothing was recorded, so the same proposal still applies.
    assert (await _apply(client, preview["proposal_id"]))[0]["verdict"] == "applied"

    card = await client.create("/api/characters", json={"name": "Dice", "first_mes": "You rolled {{roll::1d1000000}}."})
    chat = await client.create("/api/conversations", json={"character_card_id": card})
    greeting = (await client.get_json(f"/api/conversations/{chat}/messages"))[0]["id"]
    tools = await _tools(mode="chat", conversation_id=chat)
    with pytest.raises(ToolFailure, match="Edit button"):
        await tools["edit_message"].run(tools["edit_message"].arguments(message_id=greeting, mode="replace_all", text="x"))


async def test_edits_to_one_field_stack_into_one_proposal_and_a_batch_apply_skips_only_the_stale_one(
    client, library, db, monkeypatch
):
    await client.put_checked(f"/api/characters/{library['card']}", json={"description": "A harbour fence, a theif and a lier."})
    persona = await client.create("/api/user-personas", json={"name": "Wren", "description": "A ferry pilot."})
    edit = {"field": "description", "mode": "find_replace"}
    script = [
        _call("edit_character", {**edit, "find": "theif", "text": "thief"}),
        _call("edit_character", {**edit, "find": "lier", "text": "liar"}),
        _call("edit_persona", {"persona_id": persona, "field": "description", "mode": "replace_all", "text": "A pilot."}),
        _reply("Proposed three fixes."),
    ]
    calls = []

    async def complete(self, **kwargs):
        calls.append(kwargs)
        yield script.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)
    body = _body("Fix both typos in Mara's description and shorten Wren's.", character_id=library["card"])
    events = frames(await client.post_checked(RUN, json=body))
    confirms = [data["proposal"] for event, data in events if event == "confirm"]
    # The second typo fix stacks on the first: one proposal, the stored text before and both fixes after.
    assert len(confirms) == 3 and confirms[0]["proposal_id"] == confirms[1]["proposal_id"] != confirms[2]["proposal_id"]
    assert confirms[1]["fields"][0]["before"] == "A harbour fence, a theif and a lier."
    assert confirms[1]["fields"][0]["after"] == "A harbour fence, a thief and a liar."
    stacked = json.loads(calls[2]["messages"][-1]["content"])
    assert stacked["before_chars"] == stacked["after_chars"] == 36 and "on top of the edit" in stacked["note"]
    assert "(14 → 8 characters)" in [data for event, data in events if event == "tool_result"][-1]["summary"]
    assert events[-1] == ("done", {"reason": "reply", "message": ""})
    assert (await db.one("SELECT description FROM character_cards WHERE id = ?", (library["card"],)))[0].endswith("lier.")

    # The persona changes elsewhere before Apply: the card's edit applies, the persona's is reported stale.
    await client.put_checked(f"/api/user-personas/{persona}", json={"description": "A river pilot."})
    applied, stale = await _apply(client, confirms[1]["proposal_id"], confirms[2]["proposal_id"])
    assert applied["verdict"] == "applied" and stale["verdict"] == "stale"
    assert (await db.one("SELECT description FROM character_cards WHERE id = ?", (library["card"],)))[0].endswith("liar.")
    assert (await client.get_json("/api/user-personas"))[-1]["description"] == "A river pilot."

    # An edit that would take a stacked field back to its stored text is refused, and the pending one stands.
    tools = await _tools(mode="library", character_id=library["card"])
    await _propose(tools, "edit_character", **edit, find="liar", text="cheat")
    with pytest.raises(ToolFailure, match="undoing the edit already proposed"):
        await _propose(tools, "edit_character", **edit, find="cheat", text="liar")


async def test_a_follow_up_extends_the_reply_it_names_and_says_whether_anything_was_committed_since(
    client, library, db, monkeypatch
):
    calls, script = [], []

    async def complete(self, **kwargs):
        calls.append(kwargs["messages"])
        yield script.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)

    async def ask(text, resume=""):
        script[:] = [_call("read_messages", {"count": 2}), _reply("Two lines.")]
        calls.clear()
        body = _body(text, mode="chat", conversation_id=library["chat"])
        body["items"][:0] = [{"kind": "user", "text": "Earlier?"}, {"kind": "assistant", "text": "Earlier."}]
        events = frames(await client.post_checked(RUN, json={**body, "resume": resume}), resume=True)
        return events[-1][1].get("resume", ""), [list(messages) for messages in calls]

    await open_wal_anchor()
    try:
        token, first = await ask("What is said?")
        token, second = await ask("And then?", token)
        # The previous reply's last request, its tool result whole, then that reply: nothing is read again.
        assert second[0][: len(first[-1])] == first[-1] and second[0][len(first[-1])]["role"] == "assistant"
        quiet = second[0][-1]["content"]

        await db.execute("UPDATE character_cards SET description = 'Changed.' WHERE id = ?", (library["card"],))
        await db.commit()
        token, third = await ask("And then?", token)
        assert third[0][: len(second[-1])] == second[-1] and third[0][-1]["content"] != quiet

        # A token that is not the latest one replays the browser's transcript instead.
        _, stale = await ask("And then?", "f" * 32)
        assert [message["role"] for message in stale[0]] == ["system", "user", "assistant", "user"]
    finally:
        await close_wal_anchor()
