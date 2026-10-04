"""Check conversation serialization: competing streams fail via SSE; edit, delete
and branch-switch wait for the active turn to finish.
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
    """Two concurrent ``/send`` POSTs on the same conversation: the first holds the lock through writer; the second receives the
    in-band SSE error event immediately and returns.
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
    """``/edit`` waits for an in-flight ``/send`` to complete instead of racing the pipeline's view of conversation state."""
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
        resp = await streaming_client.post(f"/api/conversations/{cid}/messages/{msg_id}/edit", json={"content": "edited"})
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
    """``/stop`` aborts the in-flight LLM client and answers once the stream has settled; a subsequent ``/send`` succeeds."""
    cid = await _new_conversation(streaming_client)
    writer_gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("first")
    llm_mock.enqueue_editor(None)

    async with await _send_streaming(streaming_client, cid) as first_resp:
        assert first_resp.status_code == 200
        await writer_gate.reached.wait()
        # After the gate releases, FakeLLMClient.complete checks the abort flag and returns without yielding any payload, so the
        # SSE generator finishes -- which is what /stop waits for.
        stop = asyncio.create_task(streaming_client.post(f"/api/conversations/{cid}/stop"))
        await asyncio.sleep(0.05)
        writer_gate.release.set()
        async for _ in first_resp.aiter_lines():
            pass
        assert (await stop).json() == {"ok": True, "active": True, "settled": True}

    llm_mock.enqueue_writer("second")
    llm_mock.enqueue_editor(None)
    async with await _send_streaming(streaming_client, cid) as second_resp:
        assert second_resp.status_code == 200
        saw_error, _ = await _drain_until_error_or_done(second_resp)
    assert not saw_error, "subsequent /send saw an unexpected stream_in_progress error"


async def test_disconnect_releases_lock(streaming_client, llm_mock):
    """A streaming caller that disconnects mid-pipeline still releases the lock: CleanupStreamingResponse.__call__'s finally
    aclose()s the body iterator, which runs the sse_stream finally and releases the lock so the next caller succeeds.
    """
    cid = await _new_conversation(streaming_client)
    writer_gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("partial")
    llm_mock.enqueue_editor(None)

    async def quick_disconnect():
        async with await _send_streaming(streaming_client, cid) as resp:
            assert resp.status_code == 200
            await writer_gate.reached.wait()
            # Exit the ``async with`` without draining -- httpx closes the connection, FastAPI sees the disconnect and triggers
            # cleanup.

    task = asyncio.create_task(quick_disconnect())
    await writer_gate.reached.wait()
    writer_gate.release.set()
    await task

    # The disconnect-driven cleanup path is async and runs after the client-side ``async with`` exits, so the lock release races
    # the next /send. Poll the actual lock until cleanup releases it instead of guessing a fixed sleep, which flakes under load.
    from backend.api import deps

    async def _lock_released() -> bool:
        lock = deps._conversation_stream_locks.get(cid)
        return lock is None or not lock.locked()

    for _ in range(200):  # up to ~2s, far longer than cleanup needs
        if await _lock_released():
            break
        await asyncio.sleep(0.01)
    assert await _lock_released(), "disconnect cleanup never released the lock"

    llm_mock.enqueue_writer("recovered")
    llm_mock.enqueue_editor(None)
    async with await _send_streaming(streaming_client, cid) as resp:
        assert resp.status_code == 200
        saw_error, _ = await _drain_until_error_or_done(resp)
    assert not saw_error, "lock leaked across disconnect"


async def test_stop_during_the_save_waits_for_it_and_keeps_one_reply(streaming_client, llm_mock, monkeypatch):
    """Stop lands while the finished reply is being saved. The save completes
    once, /stop answers only after it, the stream ends as a normal turn (no
    error), and a fresh read of the conversation holds exactly that reply."""
    from backend.api import deps
    from backend.pipeline import persistence

    cid = await _new_conversation(streaming_client)
    llm_mock.enqueue_writer("The whole reply.")
    llm_mock.enqueue_editor(None)
    saving, release = asyncio.Event(), asyncio.Event()
    persist = persistence._persist_result

    async def slow_persist(*args, **kwargs):
        saving.set()
        await release.wait()
        return await persist(*args, **kwargs)

    monkeypatch.setattr(persistence, "_persist_result", slow_persist)

    events: list[str] = []
    async with await _send_streaming(streaming_client, cid) as resp:
        lines = resp.aiter_lines()
        await asyncio.wait_for(saving.wait(), 5)
        stop = asyncio.create_task(streaming_client.post(f"/api/conversations/{cid}/stop"))
        await asyncio.sleep(0.05)
        assert not stop.done(), "/stop answered before the reply was saved"
        assert deps._conversation_stream_locks[cid].locked()
        release.set()
        async for line in lines:
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
    assert (await stop).json() == {"ok": True, "active": True, "settled": True}

    assert "error" not in events
    assert events[-1] == "done"
    messages = (await streaming_client.get(f"/api/conversations/{cid}/messages")).json()
    replies = [m for m in messages if m["role"] == "assistant"]
    assert [m["content"] for m in replies] == ["The whole reply."]


async def test_a_regeneration_stopped_before_any_prose_keeps_the_original_selected(streaming_client, llm_mock):
    """No new prose, no new branch: the reply being regenerated stays the
    selected one, and the stopped attempt ends as a normal turn."""
    cid = await _new_conversation(streaming_client)
    llm_mock.enqueue_writer("The original reply.")
    llm_mock.enqueue_editor(None)
    async with await _send_streaming(streaming_client, cid) as resp:
        await _drain_until_error_or_done(resp)
    before = (await streaming_client.get(f"/api/conversations/{cid}/messages")).json()
    original = before[-1]
    assert original["content"] == "The original reply."

    writer_gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("A replacement that never streams.")
    regenerate = streaming_client.stream("POST", f"/api/conversations/{cid}/messages/{original['id']}/regenerate", json={})
    async with regenerate as resp:
        await writer_gate.reached.wait()
        llm_mock.abort()
        writer_gate.release.set()
        saw_error, _ = await _drain_until_error_or_done(resp)

    assert not saw_error
    after = (await streaming_client.get(f"/api/conversations/{cid}/messages")).json()
    assert [m["id"] for m in after] == [m["id"] for m in before]
    assert after[-1]["branch_count"] == 1
