"""Homepage stats: the persistent generated-chars counter.

The "~Tokens generated" stat must read from ``settings.generated_chars``:
seeded once from existing assistant rows (first run after the feature ships),
then advanced by ``add_generated_chars`` after each successful generation --
never recomputed from the messages table.
"""

from __future__ import annotations

import uuid
from datetime import UTC

import backend.database as dbmod


async def _add_messages(client, user_text: str, assistant_text: str) -> str:
    resp = await client.post("/api/conversations", json={"title": "Stats"})
    cid = resp.json()["id"]
    user_id, _ = await dbmod.add_message(cid, "user", user_text, 0)
    await dbmod.add_message(cid, "assistant", assistant_text, 0, parent_id=user_id)
    return cid


async def _seed_character(name: str, message_count: int, *, old: bool = False) -> str:
    """Create a conversation for *name* holding *message_count* messages.

    Pass ``old=True`` to backdate all messages by 48 hours so they satisfy the
    "missed" spotlight query's 24-hour recency cutoff.
    """
    from datetime import datetime, timedelta

    import aiosqlite

    cid = str(uuid.uuid4())
    await dbmod.create_conversation(cid, f"{name} chat", name, "")
    parent_id: int | None = None
    for i in range(message_count):
        parent_id, _ = await dbmod.add_message(cid, "user" if i % 2 == 0 else "assistant", "x", i, parent_id=parent_id)
    await dbmod.set_active_leaf(cid, parent_id)
    if old:
        import backend.database.connection as _db_conn

        cutoff = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        async with aiosqlite.connect(_db_conn.DB_PATH) as conn:
            await conn.execute(
                "UPDATE messages SET created_at = ? WHERE conversation_id = ?",
                (cutoff, cid),
            )
            await conn.commit()
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


async def test_spotlight_falls_back_to_favorite_when_nothing_qualifies(client, db):
    # A single short conversation: no character clears >100 messages, so the
    # "missed" theme is never a candidate and the favorite always shows.
    await _seed_character("Alice", 4)

    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    sp = resp.json()["character_spotlight"]
    assert sp is not None
    assert sp["theme"] == "favorite"
    assert sp["name"] == "Alice"
    assert {"theme", "name", "messages", "conversations", "card_id"} <= sp.keys()


async def test_stats_message_count_excludes_swiped_branches(client, db):
    # A linear chat of user→assistant, then an alternate assistant swipe off the
    # user message. The swipe is an off-path sibling (trash), so only the two
    # active-path messages should be counted, not three.
    cid = str(uuid.uuid4())
    await dbmod.create_conversation(cid, "Swipe chat", "Sara", "")
    u1, _ = await dbmod.add_message(cid, "user", "hi", 0, parent_id=None)
    a_active, _ = await dbmod.add_message(cid, "assistant", "active reply", 1, parent_id=u1)
    await dbmod.add_message(cid, "assistant", "swiped reply", 1, parent_id=u1)  # off-path
    await dbmod.set_active_leaf(cid, a_active)

    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_messages"] == 2
    sp = body["character_spotlight"]
    assert sp["name"] == "Sara"
    # The spotlight counts what the *character* wrote, so the one active-path
    # assistant row — not the user's turn, and not the swiped sibling. Counting
    # the user's turn here would put a solo character at double a group member's
    # total for the same output (see _CHARACTER_USAGE_CTE).
    assert sp["messages"] == 1


async def test_missed_theme_excludes_favorite(client, db, monkeypatch):
    # Alice is the clear favorite; Bob also clears >100 messages. Forcing the
    # coin flip to the last candidate must surface Bob under the "missed" theme,
    # never the favorite.  Bob's messages are backdated so he clears the 24-hour
    # recency gate in the "missed" query.
    # Seeds alternate user/assistant and the spotlight counts assistant rows
    # only, so these are 300 and 110 replies respectively.
    await _seed_character("Alice", 600)
    await _seed_character("Bob", 220, old=True)

    monkeypatch.setattr("backend.api.routes.stats.random.choice", lambda options: options[-1])

    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    sp = resp.json()["character_spotlight"]
    assert sp["theme"] == "missed"
    assert sp["name"] == "Bob"


async def test_the_spotlight_counts_a_group_member_and_a_solo_character_alike(client, db):
    """One definition of "messages": what the character wrote.

    The group arm counted assistant rows and the solo arm counted every row on the
    active path, so a cast member sat at half a solo character's total for the
    same output — never the favourite, and clearing the "missed" threshold at
    twice the play.

    Vela out-writes Nova three replies to two. Under the old asymmetry Nova's
    four active-path rows exchange Vela's three and she took the spotlight anyway.
    """
    solo = str(uuid.uuid4())
    await dbmod.create_conversation(solo, "Nova chat", "Nova", "")
    parent = None
    for i, role in enumerate(["user", "assistant", "user", "assistant"]):
        parent, _ = await dbmod.add_message(solo, role, "x", i, parent_id=parent)
    await dbmod.set_active_leaf(solo, parent)

    group = await dbmod.create_group_conversation(str(uuid.uuid4()), "Scene", [{"display_name": "Vela"}])
    member = (await dbmod.get_group_members(group["id"]))[0]
    parent = None
    for i, role in enumerate(["user", "assistant", "assistant", "assistant"]):
        parent, _ = await dbmod.add_message(
            group["id"], role, "x", i, parent_id=parent, speaker_member_id=member["id"] if role == "assistant" else None
        )
    await dbmod.set_active_leaf(group["id"], parent)

    sp = (await client.get("/api/stats")).json()["character_spotlight"]
    assert sp["name"] == "Vela", sp
    assert sp["messages"] == 3, sp
