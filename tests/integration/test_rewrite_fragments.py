"""Conversation-turn coverage for rewrite interactive fragments."""

import backend.database as dbmod
from backend.pipeline import handle_turn
from backend.pipeline.passes.editor.prompts import draft_bounds


def test_draft_bounds_quote_exact_bounded_source_excerpts():
    assert draft_bounds("  Một\n  hai.  ") == "`Một\n  hai.`"
    assert draft_bounds("a b c d e f g h i j k") == "`a b c d e...g h i j k`"
    draft = "「雨が降る。」" * 200
    assert draft_bounds(draft) == f"`{draft[:48]}...{draft[-48:]}`"


async def _drain(agen) -> list[dict]:
    return [event async for event in agen]


async def _create_fragment(client, fid: str, field_type: str, sort_order: int) -> None:
    await client.post_checked(
        "/api/interactive-fragments",
        json={
            "id": fid,
            "label": fid,
            "injection_label": fid.replace("_", " ").title(),
            "description": f"Do the {fid} edit.",
            "field_type": field_type,
            "required": False,
            "enabled": True,
            "sort_order": sort_order,
        },
    )


def _find_replace(find: str, replace: str) -> list[dict]:
    patches = [{"find": find, "replace": replace}]
    return [{"id": find, "type": "function", "function": {"name": "editor_find_replace", "arguments": {"patches": patches}}}]


def _rewrite(text: str) -> dict:
    call = {"id": "rw", "type": "function", "function": {"name": "editor_rewrite", "arguments": {"rewritten_text": text}}}
    return {"role": "assistant", "content": "", "tool_calls": [call]}


async def test_rewrite_runs_between_recasts_in_sort_order_and_replaces_the_draft(client, llm_mock):
    cid = "conv-rewrite"
    await dbmod.create_conversation(cid, "rewrite", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": True})
    await _create_fragment(client, "after_rewrite", "recast", 3)
    await _create_fragment(client, "present_tense", "rewrite", 2)
    await _create_fragment(client, "before_rewrite", "recast", 1)

    llm_mock.enqueue_writer("She walked in. She sat down.")
    llm_mock.enqueue_recast(_find_replace("walked in", "strode in"))
    # Padded: the rewrite is stripped before it replaces the draft.
    llm_mock.enqueue_editor(_rewrite("  She strides in and sits.\n"))
    llm_mock.enqueue_recast(_find_replace("sits", "takes a seat"))

    events = await _drain(handle_turn(cid, "hello"))

    edit_calls = [call for call in llm_mock.captured if call["pass"] in ("recast", "editor")]
    writer_call = next(call for call in llm_mock.captured if call["pass"] == "writer")
    assert [call["tool_choice"]["function"]["name"] for call in edit_calls] == [
        "editor_find_replace",
        "editor_rewrite",
        "editor_find_replace",
    ]
    # Forcing editor_rewrite rides the Writer's tools blob unchanged.
    assert all(call["tools"] == writer_call["tools"] for call in edit_calls)
    # Each fragment sees the draft the one before it left.
    assert [call["messages"][-2]["content"] for call in edit_calls] == [
        "She walked in. She sat down.",
        "She strode in. She sat down.",
        "She strides in and sits.",
    ]
    assert "editor_rewrite" in edit_calls[1]["messages"][-1]["content"]
    assert "## Present Tense" in edit_calls[1]["messages"][-1]["content"]

    assert [event["data"]["step"] for event in events if event.get("event") == "step_start"] == [
        "writer",
        "recast",
        "rewrite",
        "recast",
    ]
    [editor_done] = [event for event in events if event.get("event") == "editor_done"]
    assert [call["name"] for call in editor_done["data"]["tool_calls"]] == [
        "editor_find_replace",
        "editor_rewrite",
        "editor_find_replace",
    ]
    assistant = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"][-1]
    assert assistant["content"] == "She strides in and takes a seat."


async def test_structural_repetition_rewrites_without_the_length_guard(client, llm_mock):
    cid = "conv-structural"
    await dbmod.create_conversation(cid, "structural", "Bot", "a scenario")
    # The audit runs with its patch tool on and a phrase bank present; the Length Guard stays off.
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "length_guard_enabled": False, "enabled_tools": {"editor_apply_patch": True}},
    )
    await dbmod.add_phrase_group(["a sharp intake of breath"])
    # Different words, same layout: a narration paragraph, then a line of dialogue.
    earlier = 'Mara crossed the yard and counted the lamps.\n\n"Who left the gate open?" she asked.'
    draft = 'Rain hammered the mill roof while Tomas waited.\n\n"Anyone inside?" he called.'
    user, _ = await dbmod.add_message(cid, "user", "Then?", 0)
    reply, _ = await dbmod.add_message(cid, "assistant", earlier, 1, parent_id=user)
    await dbmod.set_active_leaf(cid, reply)

    llm_mock.enqueue_writer(draft)
    llm_mock.enqueue_editor(_rewrite("Lamps lit the yard one by one as she walked it, and the gate stopped her."))

    await _drain(handle_turn(cid, "And after?"))

    [editor_call] = [call for call in llm_mock.captured if call["pass"] == "editor"]
    assert editor_call["tool_choice"]["function"]["name"] == "editor_rewrite"
    prompt = editor_call["messages"][-1]["content"]
    assert "Use `editor_rewrite` to rewrite the whole draft." in prompt
    assert "limits" not in prompt
    assistant = [message for message in await dbmod.get_messages(cid) if message["role"] == "assistant"][-1]
    assert assistant["content"] == "Lamps lit the yard one by one as she walked it, and the gate stopped her."
