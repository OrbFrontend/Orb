"""A /trigger arriving while a streaming turn is mid-writer must run on its on_demand hook without waiting, then the same conversation's post_pipeline hook runs after writer releases on the same lock; both hooks RMW-increment the same workflow_state slot, so a lost write would show as final n=1 instead of 2."""

from __future__ import annotations

from backend.database import (
    add_message,
    get_workflow_state,
    set_active_leaf,
)

from ._fixtures import (
    counter_on_demand_hook,
    counter_post_pipeline_hook,
    make_workflow,
    register_for_test,
)


async def _new_conversation(streaming_client) -> str:
    resp = await streaming_client.post("/api/conversations", json={"title": "trigger-during-stream"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def test_trigger_during_stream_no_lost_writes(streaming_client, llm_mock):
    cid = await _new_conversation(streaming_client)
    wid = "counter_wf"

    msg_id, _ = await add_message(cid, "assistant", "prior", 0)
    await set_active_leaf(cid, msg_id)

    wf = make_workflow(
        wid,
        on_demand=counter_on_demand_hook(wid, "n"),
        post_pipeline=counter_post_pipeline_hook(wid, "n"),
    )

    writer_gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("response")
    llm_mock.enqueue_editor(None)

    with register_for_test(wf):

        async def consume_send():
            async with streaming_client.stream(
                "POST",
                f"/api/conversations/{cid}/send",
                json={"content": "hello", "attachments": []},
            ) as resp:
                assert resp.status_code == 200
                await writer_gate.reached.wait()
                trigger_resp = await streaming_client.post(
                    f"/api/conversations/{cid}/workflows/{wid}/trigger",
                    json={},
                )
                assert trigger_resp.status_code == 200
                mid_state = await get_workflow_state(cid, wid)
                assert mid_state == {"n": 1}, f"after trigger expected n=1, got {mid_state}"
                writer_gate.release.set()
                async for _ in resp.aiter_lines():
                    pass

        await consume_send()

    final = await get_workflow_state(cid, wid)
    assert final == {"n": 2}, f"expected n=2 after stream completed, got {final}"
