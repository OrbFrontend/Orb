"""User-message branching via the Edit & Fork route.

``POST /messages/{id}/fork-edit`` forks the conversation at a user message:
it persists an edited copy as a new sibling (same ``parent_id`` and
``turn_index``) and streams a fresh reply, leaving the original branch intact.
``get-messages`` then reports the user row as a two-branch node and
``switch-branch`` flips between the two prompts -- the regenerate sibling model
applied on the user side.
"""

from __future__ import annotations


async def _new_conversation(streaming_client) -> str:
    resp = await streaming_client.post("/api/conversations", json={"title": "fork-edit"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _drain(response) -> None:
    """Consume an SSE stream to completion so persistence and the route's
    lock-release ``finally`` block both run before the test asserts."""
    async for _ in response.aiter_lines():
        pass


async def _send(streaming_client, cid: str, content: str) -> None:
    async with streaming_client.stream(
        "POST", f"/api/conversations/{cid}/send", json={"content": content, "attachments": []}
    ) as resp:
        assert resp.status_code == 200
        await _drain(resp)


async def _fork_edit(streaming_client, cid: str, msg_id: int, content: str) -> None:
    async with streaming_client.stream(
        "POST", f"/api/conversations/{cid}/messages/{msg_id}/fork-edit", json={"content": content}
    ) as resp:
        assert resp.status_code == 200
        await _drain(resp)


async def _messages(streaming_client, cid: str) -> list[dict]:
    resp = await streaming_client.get(f"/api/conversations/{cid}/messages")
    assert resp.status_code == 200
    return resp.json()


def _only_user(msgs: list[dict]) -> dict:
    users = [m for m in msgs if m["role"] == "user"]
    assert len(users) == 1, f"expected one user message on the active path, got {len(users)}"
    return users[0]


def _reply_to(msgs: list[dict], user: dict) -> dict:
    replies = [m for m in msgs if m["role"] == "assistant" and m["parent_id"] == user["id"]]
    assert len(replies) == 1, "expected exactly one assistant reply on the active path"
    return replies[0]


async def test_fork_edit_forks_user_message(streaming_client, llm_mock):
    cid = await _new_conversation(streaming_client)

    # First turn establishes the original user message + reply.
    llm_mock.enqueue_writer("reply to original")
    llm_mock.enqueue_editor(None)
    await _send(streaming_client, cid, "original prompt")

    original_user = _only_user(await _messages(streaming_client, cid))
    assert original_user["content"] == "original prompt"
    original_id = original_user["id"]
    original_parent = original_user["parent_id"]
    original_turn = original_user["turn_index"]

    # Fork at that user message with edited text.
    llm_mock.enqueue_writer("reply to branch")
    llm_mock.enqueue_editor(None)
    await _fork_edit(streaming_client, cid, original_id, "edited prompt")

    msgs = await _messages(streaming_client, cid)
    branch_user = _only_user(msgs)
    branch_reply = _reply_to(msgs, branch_user)

    # New sibling: distinct row, same parent and turn, edited content + fresh reply.
    assert branch_user["id"] != original_id
    assert branch_user["content"] == "edited prompt"
    assert branch_user["parent_id"] == original_parent
    assert branch_user["turn_index"] == original_turn
    assert branch_reply["content"] == "reply to branch"

    # The user row is now a two-branch node pointing back at the original.
    assert branch_user["branch_count"] == 2
    assert branch_user["prev_branch_id"] == original_id
    assert branch_user["next_branch_id"] is None

    # The original branch survives and is reachable by switching back.
    resp = await streaming_client.post(f"/api/conversations/{cid}/messages/{original_id}/switch-branch", json={})
    assert resp.status_code == 200
    back = resp.json()
    back_user = _only_user(back)
    back_reply = _reply_to(back, back_user)
    assert back_user["id"] == original_id
    assert back_user["content"] == "original prompt"
    assert back_reply["content"] == "reply to original"
    assert back_user["branch_count"] == 2
    assert back_user["next_branch_id"] == branch_user["id"]


async def test_fork_edit_rejects_assistant_target(streaming_client, llm_mock):
    """fork-edit is a user-message operation; an assistant target must error
    in-band rather than fork."""
    cid = await _new_conversation(streaming_client)

    llm_mock.enqueue_writer("a reply")
    llm_mock.enqueue_editor(None)
    await _send(streaming_client, cid, "hello")

    msgs = await _messages(streaming_client, cid)
    assistant = next(m for m in msgs if m["role"] == "assistant")

    saw_error = False
    pending_event = None
    async with streaming_client.stream(
        "POST", f"/api/conversations/{cid}/messages/{assistant['id']}/fork-edit", json={"content": "nope"}
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            line = line.strip()
            if line.startswith("event:"):
                pending_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and pending_event == "error":
                saw_error = True
    assert saw_error, "fork-edit on an assistant message should yield an in-band error event"

    # No new branch was created: the assistant still has exactly one sibling.
    after = await _messages(streaming_client, cid)
    assert len([m for m in after if m["role"] == "user"]) == 1
