"""Gate /trigger during Writer streaming and verify its state update survives
the later post_pipeline hook: two increments must finish at n=2.
"""

from backend.database import add_message, get_workflow_state, set_active_leaf

from ._fixtures import counter_on_demand_hook, counter_post_pipeline_hook, make_workflow, register_for_test


async def _new_conversation(streaming_client) -> str:
    return (await streaming_client.post_checked("/api/conversations", json={"title": "trigger-during-stream"})).json()["id"]


async def test_trigger_during_stream_no_lost_writes(streaming_client, llm_mock):
    cid = await _new_conversation(streaming_client)
    wid = "counter_wf"

    msg_id, _ = await add_message(cid, "assistant", "prior", 0)
    await set_active_leaf(cid, msg_id)

    wf = make_workflow(wid, on_demand=counter_on_demand_hook(wid, "n"), post_pipeline=counter_post_pipeline_hook(wid, "n"))

    writer_gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("response")
    llm_mock.enqueue_editor(None)

    with register_for_test(wf):

        async def consume_send():
            async with streaming_client.stream(
                "POST", f"/api/conversations/{cid}/send", json={"content": "hello", "attachments": []}
            ) as resp:
                assert resp.status_code == 200
                await writer_gate.reached.wait()
                await streaming_client.post_checked(f"/api/conversations/{cid}/workflows/{wid}/trigger", json={})
                mid_state = await get_workflow_state(cid, wid)
                assert mid_state == {"n": 1}, f"after trigger expected n=1, got {mid_state}"
                writer_gate.release.set()
                async for _ in resp.aiter_lines():
                    pass

        await consume_send()

    final = await get_workflow_state(cid, wid)
    assert final == {"n": 2}, f"expected n=2 after stream completed, got {final}"
