"""The batched Inspector read that backs the in-chat Inspector blocks."""

from __future__ import annotations

import backend.database as dbmod


async def _reply_with_log(cid: str, parent: int, turn: int, injection: str) -> int:
    reply, _ = await dbmod.add_message(
        cid,
        "assistant",
        f"reply {turn}",
        turn,
        parent_id=parent,
        decision_evaluations={"version": 2, "evaluations": [], "skipped": []},
    )
    await dbmod.add_conversation_log(cid, turn, [{"name": "direct_scene"}], ["tense"], injection, 40 + turn, message_id=reply)
    return reply


async def test_batch_matches_single_reads(client, db):
    cid = "conv-batch-logs"
    await dbmod.create_conversation(cid, "Batch", "Bot", "a scenario")
    u1, _ = await dbmod.add_message(cid, "user", "hello", 0)
    a1 = await _reply_with_log(cid, u1, 1, "first block")
    u2, _ = await dbmod.add_message(cid, "user", "again", 2, parent_id=a1)
    a2 = await _reply_with_log(cid, u2, 3, "second block")
    # A retried log for the same reply: the newest row wins, as in the single read.
    await dbmod.add_conversation_log(cid, 3, [], ["tense"], "second block, retried", 9, message_id=a2)
    await dbmod.add_state_events(
        cid,
        a2,
        [{"fragment_id": "hp", "entry_id": "e-hp", "op": "add", "text": "5", "fragment_label": "HP", "source": "agent"}],
    )
    # The user message has no log: it gets the fallback shape.
    resp = await client.get(f"/api/conversations/{cid}/director-logs", params={"ids": f"{a1},{a2},{u2}"})
    assert resp.status_code == 200
    batch = resp.json()
    assert set(batch) == {str(a1), str(a2), str(u2)}
    for mid in (a1, a2, u2):
        single = (await client.get(f"/api/conversations/{cid}/messages/{mid}/director-log")).json()
        assert batch[str(mid)] == single
    assert batch[str(a2)]["injection_block"] == "second block, retried"
    assert [c["text"] for c in batch[str(a2)]["state"]["changes"]] == ["5"]
    assert batch[str(u2)]["mood_data_available"] is False


async def test_batch_leaves_out_foreign_and_unknown_ids(client, db):
    await dbmod.create_conversation("conv-a", "A", "Bot", "s")
    await dbmod.create_conversation("conv-b", "B", "Bot", "s")
    ua, _ = await dbmod.add_message("conv-a", "user", "hi", 0)
    ub, _ = await dbmod.add_message("conv-b", "user", "hi", 0)
    foreign = await _reply_with_log("conv-b", ub, 1, "not yours")
    own = await _reply_with_log("conv-a", ua, 1, "yours")

    resp = await client.get("/api/conversations/conv-a/director-logs", params={"ids": f"{own},{foreign},999999"})
    assert resp.status_code == 200
    assert list(resp.json()) == [str(own)]


async def test_batch_rejects_bad_ids(client, db):
    await dbmod.create_conversation("conv-bad", "Bad", "Bot", "s")
    assert (await client.get("/api/conversations/conv-bad/director-logs", params={"ids": "1,x"})).status_code == 422
    too_many = ",".join(str(i) for i in range(1, 200))
    assert (await client.get("/api/conversations/conv-bad/director-logs", params={"ids": too_many})).status_code == 422
    empty = await client.get("/api/conversations/conv-bad/director-logs")
    assert empty.status_code == 200 and empty.json() == {}
