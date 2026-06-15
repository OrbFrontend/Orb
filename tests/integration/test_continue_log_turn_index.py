"""Regression: the /continue path (handle_turn with skip_user_persist=True)
must file its conversation_logs row at the *user* turn, exactly like a normal
/send turn — not one turn higher (the assistant turn).

A normal turn logs at ``next_turn`` which, for a fresh send, equals the user
message's turn_index. With ``skip_user_persist=True`` the user row already
exists at turn T, so ``next_turn = T + 1`` is the assistant's turn. Filing the
log there contradicts ``_conversation_log_writer``'s contract ("the user turn
for a fresh turn") and lands the row one turn off versus every other
fresh-turn path.
"""

from __future__ import annotations

import backend.database as dbmod
from backend.orchestrator import handle_turn


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


async def test_continue_logs_at_user_turn(client, db, llm_mock):
    # `client` patches DB_PATH + runs init_db; `llm_mock` swaps in the fake LLM.
    cid = "conv-continue"
    await dbmod.create_conversation(cid, "continue", "Bot", "a scenario")

    # Seed a dangling user message as the active leaf — the state /continue
    # operates on (user typed, no reply generated yet).
    user_id, _ = await dbmod.add_message(cid, "user", "hello there", 0, parent_id=None)
    await dbmod.set_active_leaf(cid, user_id)

    # Default settings enable no pre-writer tools and no audit, so only the
    # writer pass runs: one enqueued writer reply is all the pipeline needs.
    llm_mock.enqueue_writer("a generated reply")

    await _drain(handle_turn(cid, "hello there", skip_user_persist=True))

    # The assistant reply landed at the next turn (user turn + 1).
    messages = await dbmod.get_messages(cid)
    user_msg = next(m for m in messages if m["role"] == "user")
    asst_msg = next(m for m in messages if m["role"] == "assistant")
    assert user_msg["turn_index"] == 0
    assert asst_msg["turn_index"] == 1

    # The conversation log for this turn must be filed at the *user* turn (0),
    # matching the normal /send convention — not the assistant turn (1).
    logs = await dbmod.get_conversation_logs(cid)
    assert len(logs) == 1, f"expected exactly one log row, got {len(logs)}"
    assert logs[0]["turn_index"] == user_msg["turn_index"], (
        f"conversation log filed at turn {logs[0]['turn_index']}, expected user turn {user_msg['turn_index']}"
    )
