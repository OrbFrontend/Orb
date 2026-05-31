"""Pins per-conversation serialization of the streaming pipeline.

Streaming routes (``/send``, ``/continue``, ``/regenerate``,
``/super_regenerate``, ``/magic_rewrite``) and the non-streaming
mutators that share state with an in-flight turn (``/edit``,
``/delete``, ``/switch-branch``) share ``_conversation_stream_locks``.
The streaming side surfaces contention as an in-band SSE
``event: error\\ndata: Another generation is already running`` so the
client fails fast instead of queueing; the mutator side blocks on
``async with`` because those routes have no SSE channel and the caller
expects the action to land rather than fail.

Exercising one streaming route is enough to pin the lock contract;
per-route correctness is the domain of those routes' own tests.
"""

from __future__ import annotations

import asyncio


async def _new_conversation(streaming_client) -> str:
    resp = await streaming_client.post("/api/conversations", json={"title": "stream-conc"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _send_streaming(streaming_client, cid: str, content: str = "hi"):
    return streaming_client.stream("POST", f"/api/conversations/{cid}/send", json={"content": content, "attachments": []})


async def _drain_until_error_or_done(response) -> tuple[bool, str | None]:
    saw_error = False
    error_data: str | None = None
    pending_event: str | None = None
    async for line in response.aiter_lines():
        line = line.strip()
        if line.startswith("event:"):
            pending_event = line.split(":", 1)[1].strip()
            continue
        if line.startswith("data:") and pending_event == "error":
            saw_error = True
            error_data = line.split(":", 1)[1].strip()
            break
        if line.startswith("data:") and pending_event == "done":
            break
    return saw_error, error_data


async def test_second_concurrent_send_yields_inline_error_event(streaming_client, llm_mock):
    """Two concurrent ``/send`` POSTs on the same conversation: the first
    holds the lock through writer; the second receives the in-band SSE
    error event immediately and returns.
    """
    cid = await _new_conversation(streaming_client)

    writer_gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("first response")
    llm_mock.enqueue_editor(None)

    async def consume_second():
        await writer_gate.reached.wait()
        async with await _send_streaming(streaming_client, cid) as resp:
            assert resp.status_code == 200
            return await _drain_until_error_or_done(resp)

    second_task = asyncio.create_task(consume_second())
    async with await _send_streaming(streaming_client, cid) as first_resp:
        assert first_resp.status_code == 200
        await writer_gate.reached.wait()
        saw_error, error_data = await second_task
        writer_gate.release.set()
        # Drain the first stream so its lock-release in ``finally`` runs.
        async for _ in first_resp.aiter_lines():
            pass

    assert saw_error, "second concurrent /send did not produce an in-band error event"
    assert error_data is not None and "Another generation is already running" in error_data


async def test_edit_blocks_during_stream(streaming_client, llm_mock):
    """``/edit`` waits for an in-flight ``/send`` to complete instead of
    racing the pipeline's view of conversation state.
    """
    cid = await _new_conversation(streaming_client)
    writer_gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("hi")
    llm_mock.enqueue_editor(None)

    from backend.database import add_message, set_active_leaf

    msg_id, _ = await add_message(cid, "user", "original", 0)
    await set_active_leaf(cid, msg_id)

    edit_started = asyncio.Event()
    edit_completed = asyncio.Event()

    async def fire_edit():
        edit_started.set()
        resp = await streaming_client.post(
            f"/api/conversations/{cid}/messages/{msg_id}/edit",
            json={"content": "edited"},
        )
        edit_completed.set()
        return resp

    async with await _send_streaming(streaming_client, cid) as resp:
        assert resp.status_code == 200
        await writer_gate.reached.wait()
        edit_task = asyncio.create_task(fire_edit())
        await edit_started.wait()
        await asyncio.sleep(0.05)
        assert not edit_completed.is_set(), "/edit returned while stream still held the lock"
        writer_gate.release.set()
        async for _ in resp.aiter_lines():
            pass

    edit_resp = await edit_task
    assert edit_resp.status_code == 200


async def test_stop_releases_lock(streaming_client, llm_mock):
    """``/stop`` aborts the in-flight LLM client; the SSE finally releases
    the lock; a subsequent ``/send`` succeeds.
    """
    cid = await _new_conversation(streaming_client)
    writer_gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("first")
    llm_mock.enqueue_editor(None)

    async with await _send_streaming(streaming_client, cid) as first_resp:
        assert first_resp.status_code == 200
        await writer_gate.reached.wait()
        # /stop calls abort() on every registered LLM client (see
        # backend/main.py:_active_clients). After the gate releases,
        # FakeLLMClient.complete checks the abort flag and returns
        # without yielding any payload, so the SSE generator finishes.
        await streaming_client.post(f"/api/conversations/{cid}/stop")
        writer_gate.release.set()
        async for _ in first_resp.aiter_lines():
            pass

    llm_mock.enqueue_writer("second")
    llm_mock.enqueue_editor(None)
    async with await _send_streaming(streaming_client, cid) as second_resp:
        assert second_resp.status_code == 200
        saw_error, _ = await _drain_until_error_or_done(second_resp)
    assert not saw_error, "subsequent /send saw an unexpected stream_in_progress error"


async def test_disconnect_releases_lock(streaming_client, llm_mock):
    """A streaming caller that disconnects mid-pipeline still releases
    the lock: _CleanupStreamingResponse.__call__'s finally aclose()s the
    body iterator, which runs the _sse_stream finally and releases the
    lock so the next caller succeeds.
    """
    cid = await _new_conversation(streaming_client)
    writer_gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("partial")
    llm_mock.enqueue_editor(None)

    async def quick_disconnect():
        async with await _send_streaming(streaming_client, cid) as resp:
            assert resp.status_code == 200
            await writer_gate.reached.wait()
            # Exit the ``async with`` without draining -- httpx closes
            # the connection, FastAPI sees the disconnect and triggers
            # cleanup.

    task = asyncio.create_task(quick_disconnect())
    await writer_gate.reached.wait()
    writer_gate.release.set()
    await task

    # The disconnect-driven cleanup path is async and runs after the
    # client-side ``async with`` exits, so the lock release races the
    # next /send. Sleep to let it land before retrying.
    await asyncio.sleep(0.05)

    llm_mock.enqueue_writer("recovered")
    llm_mock.enqueue_editor(None)
    async with await _send_streaming(streaming_client, cid) as resp:
        assert resp.status_code == 200
        saw_error, _ = await _drain_until_error_or_done(resp)
    assert not saw_error, "lock leaked across disconnect"
