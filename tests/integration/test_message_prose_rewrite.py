"""The saved-message Prose Rewriter endpoint."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from backend import database as dbmod
from backend.api.routes import messages as message_routes
from backend.inference import AbortToken
from backend.pipeline import handle_turn

pytestmark = pytest.mark.asyncio


async def _assistant_message(cid: str, content: str, *, writer_draft: str | None = None) -> int:
    await dbmod.create_conversation(cid, "Prose", "Bot", "")
    message_id, _ = await dbmod.add_message(
        cid,
        "assistant",
        content,
        0,
        writer_draft=content if writer_draft is None else writer_draft,
        advance_leaf=True,
    )
    return message_id


async def _drain(agen) -> list[dict]:
    return [event async for event in agen]


def _enable(monkeypatch) -> None:
    monkeypatch.setattr(message_routes, "resolve_prose_rewrite", lambda _settings: {"variant_id": "test", "gpu": False})


def _done_event(response) -> dict:
    return json.loads(response.text.split("event: prose_rewrite_done\ndata: ", 1)[1].split("\n\n", 1)[0])


async def _content(db, message_id: int) -> str:
    async with db.execute("SELECT content FROM messages WHERE id = ?", (message_id,)) as cursor:
        return (await cursor.fetchone())["content"]


def _stream(cid: str, message_id: int, token):
    return message_routes._stream_prose_rewrite_message(cid, message_id, {"variant_id": "test", "gpu": False}, token)


async def test_rewrites_saved_assistant_message_and_stales_its_proposals(client, db, monkeypatch):
    cid = "message-prose-rewrite"
    message_id = await _assistant_message(cid, "Editor-final reply.", writer_draft="Original Writer draft.")
    _enable(monkeypatch)

    sources: list[str] = []

    async def fake_rewrite(source, _config):
        sources.append(source)
        yield {"type": "draft_update", "draft": "Rewritten reply."}
        yield {"type": "rewritten", "draft": "Rewritten reply."}

    stale_ids: list[list[int]] = []

    async def mark_stale(ids):
        stale_ids.append(ids)
        return 0

    monkeypatch.setattr(message_routes, "prose_rewrite_step", fake_rewrite)
    monkeypatch.setattr(message_routes, "mark_changesets_stale_for_messages", mark_stale)

    response = await client.post(f"/api/conversations/{cid}/messages/{message_id}/prose-rewrite", json={})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: prose_rewrite_update" in response.text
    assert "event: prose_rewrite_done" in response.text
    done = _done_event(response)
    assert done == {"message_id": message_id, "content": "Rewritten reply.", "changed": True, "warning": ""}
    assert sources == ["Original Writer draft."]
    assert stale_ids == [[message_id]]
    assert await _content(db, message_id) == "Rewritten reply."


async def test_streams_a_snapshot_before_persisting_the_rewrite(streaming_client, db, monkeypatch):
    cid = "message-prose-live"
    message_id = await _assistant_message(cid, "Original reply.")
    _enable(monkeypatch)
    snapshot_sent = asyncio.Event()
    finish = asyncio.Event()

    async def fake_rewrite(_source, _config):
        yield {"type": "draft_update", "draft": "First streamed snapshot."}
        snapshot_sent.set()
        await finish.wait()
        yield {"type": "rewritten", "draft": "Final rewritten reply."}

    monkeypatch.setattr(message_routes, "prose_rewrite_step", fake_rewrite)

    try:
        async with streaming_client.stream(
            "POST",
            f"/api/conversations/{cid}/messages/{message_id}/prose-rewrite",
            json={},
        ) as response:
            assert response.status_code == 200
            lines = response.aiter_lines()
            while True:
                line = await anext(lines)
                if line == "event: prose_rewrite_update":
                    break
            update = json.loads((await anext(lines)).removeprefix("data: "))
            assert update == {"message_id": message_id, "draft": "First streamed snapshot."}
            await snapshot_sent.wait()
            assert await _content(db, message_id) == "Original reply."

            finish.set()
            body = "\n".join([line async for line in lines])
    finally:
        finish.set()

    assert "event: prose_rewrite_done" in body
    assert await _content(db, message_id) == "Final rewritten reply."


async def test_abort_keeps_the_saved_message_unchanged(client, db, monkeypatch):
    cid = "message-prose-abort"
    message_id = await _assistant_message(cid, "Editor-final reply.", writer_draft="Original Writer draft.")
    continue_rewrite = asyncio.Event()

    async def fake_rewrite(_source, _config):
        yield {"type": "draft_update", "draft": "Partial streamed snapshot."}
        await continue_rewrite.wait()
        yield {"type": "rewritten", "draft": "This must not persist."}

    monkeypatch.setattr(message_routes, "prose_rewrite_step", fake_rewrite)
    token = AbortToken()
    stream = _stream(cid, message_id, token)

    first = await anext(stream)
    assert first["event"] == "prose_rewrite_update"
    token.abort()
    done = await anext(stream)
    assert done["data"]["aborted"] is True
    continue_rewrite.set()
    await stream.aclose()
    assert await _content(db, message_id) == "Editor-final reply."


async def test_stream_loads_the_current_message_after_acquiring_its_lock(client, db, monkeypatch):
    cid = "message-prose-fresh-read"
    message_id = await _assistant_message(cid, "Before edit.", writer_draft="Original Writer draft.")

    async def waiting_rewrite(_source, _config):
        await asyncio.Event().wait()
        yield {"type": "rewritten", "draft": "Unreachable"}

    monkeypatch.setattr(message_routes, "prose_rewrite_step", waiting_rewrite)
    token = AbortToken()
    stream = _stream(cid, message_id, token)

    # Creating an async generator does not run it. This models an edit that
    # wins the conversation lock after the request is validated but before the
    # SSE layer starts the rewrite generator.
    await dbmod.update_message_content(message_id, "Edit that won the lock.")
    token.abort()
    done = await anext(stream)

    assert done["data"]["content"] == "Edit that won the lock."
    assert await _content(db, message_id) == "Edit that won the lock."


async def test_rejects_a_user_message(client):
    cid = "message-prose-user"
    await dbmod.create_conversation(cid, "Prose", "Bot", "")
    message_id, _ = await dbmod.add_message(cid, "user", "Do not rewrite me.", 0, advance_leaf=True)

    response = await client.post(f"/api/conversations/{cid}/messages/{message_id}/prose-rewrite", json={})

    assert response.status_code == 400
    assert response.json()["detail"] == "Only assistant messages can be rewritten"


async def test_keeps_the_message_when_the_local_rewriter_warns(client, db, monkeypatch):
    cid = "message-prose-warning"
    message_id = await _assistant_message(cid, "Editor-final reply.", writer_draft="Original Writer draft.")
    _enable(monkeypatch)

    async def failed_rewrite(_source, _config):
        yield {"type": "warning", "reason": "The local model stopped"}
        yield {"type": "rewritten", "draft": "Original Writer draft."}

    monkeypatch.setattr(message_routes, "prose_rewrite_step", failed_rewrite)

    response = await client.post(f"/api/conversations/{cid}/messages/{message_id}/prose-rewrite", json={})

    assert response.status_code == 200
    done = _done_event(response)
    assert done == {
        "message_id": message_id,
        "content": "Editor-final reply.",
        "changed": False,
        "warning": "The local model stopped",
    }
    assert await _content(db, message_id) == "Editor-final reply."


async def test_rejects_messages_without_a_retained_writer_draft(client):
    cid = "message-prose-legacy"
    await dbmod.create_conversation(cid, "Prose", "Bot", "")
    message_id, _ = await dbmod.add_message(cid, "assistant", "Legacy reply.", 0, advance_leaf=True)

    response = await client.post(f"/api/conversations/{cid}/messages/{message_id}/prose-rewrite", json={})

    assert response.status_code == 409
    assert response.json()["detail"] == "Original Writer draft is unavailable for this message"


async def test_pipeline_persists_writer_draft_before_later_stages(client, db, llm_mock):
    cid = "message-prose-capture"
    await dbmod.create_conversation(cid, "Prose", "Bot", "")
    llm_mock.enqueue_writer("Raw Writer draft.")

    async def rewritten_by_later_stage(_cfg, state, **_kwargs):
        state.resp_text = "Editor-final reply."
        yield {"event": "writer_rewrite", "data": {"refined_text": state.resp_text}}

    with patch("backend.pipeline.orchestrator.editor_stage", new=rewritten_by_later_stage):
        await _drain(handle_turn(cid, "hello"))

    async with db.execute(
        "SELECT content, writer_draft FROM messages WHERE conversation_id = ? AND role = 'assistant'", (cid,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["content"] == "Editor-final reply."
    assert row["writer_draft"] == "Raw Writer draft."


async def test_noop_rewrite_uses_the_macro_frozen_writer_draft(client, db, llm_mock, monkeypatch):
    cid = "message-prose-macros"
    await dbmod.create_conversation(cid, "Prose", "Bot", "")
    llm_mock.enqueue_writer("The sky turns {{random::gold::silver}} tonight.")

    await _drain(handle_turn(cid, "hello"))
    async with db.execute(
        "SELECT id, content, writer_draft FROM messages WHERE conversation_id = ? AND role = 'assistant'", (cid,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert "{{random" not in row["content"]
    assert row["writer_draft"] == row["content"]

    seen_sources: list[str] = []

    async def no_op_rewrite(source, _config):
        seen_sources.append(source)
        yield {"type": "rewritten", "draft": source}

    _enable(monkeypatch)
    monkeypatch.setattr(message_routes, "prose_rewrite_step", no_op_rewrite)

    response = await client.post(f"/api/conversations/{cid}/messages/{row['id']}/prose-rewrite", json={})

    assert response.status_code == 200
    done = _done_event(response)
    assert done == {"message_id": row["id"], "content": row["content"], "changed": False, "warning": ""}
    assert seen_sources == [row["content"]]
    assert await _content(db, row["id"]) == row["content"]


async def test_compression_preserves_retained_writer_drafts(client, db):
    cid = "message-prose-compress"
    await dbmod.create_conversation(cid, "Prose", "Bot", "")
    user_id, _ = await dbmod.add_message(cid, "user", "Prompt", 0, advance_leaf=True)
    assistant_id, _ = await dbmod.add_message(
        cid,
        "assistant",
        "Editor-final reply.",
        1,
        parent_id=user_id,
        writer_draft="Original Writer draft.",
        advance_leaf=True,
    )
    assert assistant_id

    response = await client.post(f"/api/conversations/{cid}/compress", json={"summary": "Earlier events.", "keep_count": 2})

    assert response.status_code == 200
    new_cid = response.json()["new_conversation_id"]
    # Read the column, not the wire: the list routes project it away (see
    # ``_for_the_client``), and what this test is about is the fork carrying
    # the text across, byte for byte.
    async with db.execute(
        "SELECT writer_draft FROM messages WHERE conversation_id = ? AND content = ?",
        (new_cid, "Editor-final reply."),
    ) as cursor:
        assert (await cursor.fetchone())["writer_draft"] == "Original Writer draft."
    # And the client still learns the button has a source, without being sent one.
    messages = (await client.get(f"/api/conversations/{new_cid}/messages")).json()
    retained_assistant = next(message for message in messages if message["content"] == "Editor-final reply.")
    assert retained_assistant["has_writer_draft"] is True
    assert "writer_draft" not in retained_assistant
