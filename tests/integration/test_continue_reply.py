"""Continue resends a reply's raw text-mode prompt and thought byte for byte, then grows the same row."""

import backend.database as dbmod
from backend.pipeline import handle_continue_reply


async def _cut_reply(client, cid: str, record_model: str | None) -> tuple[int, int]:
    ep = await client.post_json("/api/endpoints", json={"url": "http://llama.local", "api_key": ""})
    await client.put(f"/api/endpoints/{ep['id']}", json={"completion_mode": "text"})
    await client.put("/api/settings", json={"active_endpoint_id": ep["id"]})
    await dbmod.create_conversation(cid, "continue", "Bot", "")
    user_id, _ = await dbmod.add_message(cid, "user", "hello", 0, parent_id=None)
    reply_id, _ = await dbmod.add_message(cid, "assistant", "She walked to the ", 1, parent_id=user_id)
    await dbmod.set_active_leaf(cid, reply_id)
    model = (await dbmod.get_settings())["model_name"] if record_model is None else record_model
    await dbmod.set_reply_prompt(cid, reply_id, model, "<prompt>", "<thought>plan<close>")
    return user_id, reply_id


async def test_continue_resends_raw_prompt_and_appends(client, db, llm_mock):
    cid = "conv-continue-reply"
    user_id, reply_id = await _cut_reply(client, cid, None)
    llm_mock.enqueue_raw(" door.")

    events = [ev async for ev in handle_continue_reply(cid, reply_id)]

    assert events[-1] == {"event": "done"}, events
    assert [call["prompt"] for call in llm_mock.raw_calls] == ["<prompt><thought>plan<close>She walked to the"]
    messages = await dbmod.get_messages(cid)
    assert [(m["id"], m["content"]) for m in messages] == [(user_id, "hello"), (reply_id, "She walked to the door.")]


async def test_continue_rebuilds_the_prompt_for_another_models_reply(client, db, llm_mock):
    cid = "conv-continue-other-model"
    _, reply_id = await _cut_reply(client, cid, "some-other-model")
    llm_mock.enqueue_writer(" door.")

    events = [ev async for ev in handle_continue_reply(cid, reply_id)]

    assert events[-1] == {"event": "done"}, events
    assert llm_mock.raw_calls == []
    call = llm_mock.captured[-1]
    assert call["params"]["prefill"] == "She walked to the"
    assert [m["content"] for m in call["messages"] if m["role"] != "system"][-1:] == ["hello"]
    assert (await dbmod.get_message_by_id(reply_id))["content"] == "She walked to the door."


async def test_editing_an_earlier_message_drops_the_stored_prompt(client, db, llm_mock):
    cid = "conv-continue-edited-history"
    user_id, reply_id = await _cut_reply(client, cid, None)

    await dbmod.save_message_edit(await dbmod.get_message_by_id(reply_id), "She walked")
    assert await dbmod.get_reply_prompt(cid) is not None
    await dbmod.save_message_edit(await dbmod.get_message_by_id(user_id), "hello there")
    assert await dbmod.get_reply_prompt(cid) is None
