"""Homepage stats: the persistent generated-chars counter.

The "~Tokens generated" stat must read from ``settings.generated_chars``:
seeded once from existing assistant rows (first run after the feature ships),
then advanced by ``add_generated_chars`` after each successful generation --
never recomputed from the messages table.
"""

from __future__ import annotations

import backend.database as dbmod


async def _add_messages(client, user_text: str, assistant_text: str) -> str:
    resp = await client.post("/api/conversations", json={"title": "Stats"})
    cid = resp.json()["id"]
    user_id, _ = await dbmod.add_message(cid, "user", user_text, 0)
    await dbmod.add_message(cid, "assistant", assistant_text, 0, parent_id=user_id)
    return cid


async def test_counter_seeds_from_assistant_rows_on_first_read(client, db):
    await _add_messages(client, "u" * 10, "a" * 40)

    assert await dbmod.get_generated_chars() == 40

    # The seed is persisted on the settings row, not recomputed per read.
    async with db.execute("SELECT generated_chars FROM settings WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert row["generated_chars"] == 40


async def test_counter_is_lifetime_and_survives_conversation_deletion(client, db):
    cid = await _add_messages(client, "u" * 10, "a" * 40)
    assert await dbmod.get_generated_chars() == 40

    await client.delete(f"/api/conversations/{cid}")

    # A recompute-from-DB would drop to 0 here; the lifetime counter must not.
    assert await dbmod.get_generated_chars() == 40


async def test_increment_after_seed_adds_exactly_the_new_chars(client, db):
    await _add_messages(client, "u" * 10, "a" * 40)
    assert await dbmod.get_generated_chars() == 40

    await dbmod.add_generated_chars(25)
    assert await dbmod.get_generated_chars() == 65


async def test_first_increment_on_unseeded_counter_does_not_double_count(client, db):
    # The orchestrator credits the turn AFTER persisting the assistant row. If
    # the counter was never seeded, that row is already inside the seed scan,
    # so the increment for this one turn must be absorbed, not added on top.
    await _add_messages(client, "u" * 10, "a" * 40)

    await dbmod.add_generated_chars(40)
    assert await dbmod.get_generated_chars() == 40


async def test_stats_endpoint_derives_tokens_from_counter(client, db):
    await _add_messages(client, "u" * 10, "a" * 40)

    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["estimated_tokens"] == 10  # 40 chars / CHARS_PER_TOKEN(4)
    # "Words written" still comes from user-typed chars only.
    assert body["total_words"] == 2  # 10 chars / 5
