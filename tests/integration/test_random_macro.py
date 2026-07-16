"""Turn-level integration tests for {{random}} / inline-macro fixing.

The design under test: message rows resolve inline macros once at the persist
boundary (user send, assistant persist, plain edit), greetings re-roll from a
stashed raw template on every fetch until the first user message freezes them,
mood fragment text resolves against the per-conversation
``director_state.macro_choices`` map so a pick is made once and reused every
later turn (and carried by checkpoint), and director-authored interactive
values roll fresh on every emission.
"""

from __future__ import annotations

import json

import backend.database as dbmod
from backend.pipeline import handle_turn

_GREETING_OPTIONS = {"Ahoy", "Wotcher"}


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


def _injection_block(events: list[dict]) -> str:
    blocks = [e["data"]["injection_block"] for e in events if e.get("event") == "director_done"]
    return blocks[-1] if blocks else ""


async def _greeting_contents(client, cid: str) -> list[str]:
    resp = await client.get(f"/api/conversations/{cid}/messages")
    assert resp.status_code == 200
    return [m["content"] for m in resp.json() if m["turn_index"] == 0]


# ── persist boundary: user + assistant + edit ────────────────────────────────


async def test_send_persists_user_message_resolved(client, db, llm_mock):
    cid = "conv-rm-send"
    await dbmod.create_conversation(cid, "rm", "Bot", "a scenario")
    llm_mock.enqueue_writer("Understood.")

    # 2d1 and a single-option random resolve deterministically.
    events = await _drain(handle_turn(cid, "I rolled {{roll::2d1}} and go {{random::north}}"))

    created = [e for e in events if e.get("event") == "user_message_created"]
    assert len(created) == 1
    assert created[0]["data"]["content"] == "I rolled 2 and go north"

    async with db.execute("SELECT content FROM messages WHERE conversation_id = ? AND role = 'user'", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["content"] == "I rolled 2 and go north"


async def test_assistant_reply_macros_fixed_at_persist(client, db, llm_mock):
    cid = "conv-rm-asst"
    await dbmod.create_conversation(cid, "rm", "Bot", "a scenario")
    llm_mock.enqueue_writer("The sky turns {{random::gold::silver}} tonight.")

    await _drain(handle_turn(cid, "hello"))

    async with db.execute("SELECT content FROM messages WHERE conversation_id = ? AND role = 'assistant'", (cid,)) as cur:
        row = await cur.fetchone()
    assert "{{random" not in row["content"]
    assert row["content"] in {f"The sky turns {w} tonight." for w in ("gold", "silver")}


async def test_plain_edit_resolves_inline_macros(client, db, llm_mock):
    cid = "conv-rm-edit"
    await dbmod.create_conversation(cid, "rm", "Bot", "a scenario")
    llm_mock.enqueue_writer("ok")
    await _drain(handle_turn(cid, "hello"))
    msgs = await dbmod.get_messages(cid)
    user_id = next(m["id"] for m in msgs if m["role"] == "user")

    resp = await client.post(
        f"/api/conversations/{cid}/messages/{user_id}/edit",
        json={"content": "changed to {{roll::3d1}}"},
    )
    assert resp.status_code == 200

    async with db.execute("SELECT content FROM messages WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    assert row["content"] == "changed to 3"


# ── greetings: re-roll until first user message, then frozen ─────────────────


async def test_greeting_rerolls_until_first_user_message(client, db, llm_mock):
    resp = await client.post(
        "/api/conversations",
        json={"title": "g", "character_name": "Bot", "first_mes": "{{random::Ahoy::Wotcher}}, {{user}}!"},
    )
    cid = resp.json()["id"]

    # Template stashed raw; content resolved.
    async with db.execute(
        "SELECT content, json_extract(workflow_state, '$.macros.template') AS template FROM messages WHERE conversation_id = ?",
        (cid,),
    ) as cur:
        row = await cur.fetchone()
    assert row["template"] == "{{random::Ahoy::Wotcher}}, {{user}}!"
    assert row["content"].split(",")[0] in _GREETING_OPTIONS

    # Unfrozen: repeated fetches re-roll from the template. 40 draws of a fair
    # 2-option pick miss one side with probability 2^-40 — not a flake source.
    seen = set()
    for _ in range(40):
        (content,) = await _greeting_contents(client, cid)
        assert "{{random" not in content
        seen.add(content.split(",")[0])
    assert seen == _GREETING_OPTIONS

    # First user message freezes the greeting at its last-served resolution.
    (before,) = await _greeting_contents(client, cid)
    llm_mock.enqueue_writer("hi")
    await _drain(handle_turn(cid, "hello there"))
    for _ in range(5):
        contents = await _greeting_contents(client, cid)
        assert before in contents  # the replied-to greeting kept its bytes


async def test_alternate_greetings_resolved_with_templates(client, db):
    await dbmod.create_character_card(
        {
            "id": "card-rm",
            "name": "Bot",
            "first_mes": "Main {{random::A::B}}",
            "alternate_greetings": ["Alt {{random::C::D}}", "Alt plain"],
        }
    )
    resp = await client.post("/api/conversations", json={"title": "g", "character_card_id": "card-rm"})
    cid = resp.json()["id"]

    async with db.execute("SELECT content, workflow_state FROM messages WHERE conversation_id = ? ORDER BY id", (cid,)) as cur:
        rows = await cur.fetchall()
    by_content = {r["content"]: r["workflow_state"] for r in rows}
    assert len(rows) == 3
    assert all("{{random" not in c for c in by_content)
    # Macro greetings carry their raw template; the plain one carries nothing.
    templates = {json.loads(ws)["macros"]["template"] for ws in by_content.values() if ws is not None}
    assert templates == {"Main {{random::A::B}}", "Alt {{random::C::D}}"}
    assert by_content["Alt plain"] is None


async def test_editing_greeting_drops_template_and_stops_reroll(client, db):
    resp = await client.post(
        "/api/conversations",
        json={"title": "g", "character_name": "Bot", "first_mes": "Hi {{random::X::Y}}"},
    )
    cid = resp.json()["id"]
    msgs = await dbmod.get_messages(cid)
    greeting_id = msgs[0]["id"]

    resp = await client.post(f"/api/conversations/{cid}/messages/{greeting_id}/edit", json={"content": "Hand-written opening"})
    assert resp.status_code == 200

    # No template left, so fetches (still unfrozen — no user message) keep the edit.
    for _ in range(5):
        assert await _greeting_contents(client, cid) == ["Hand-written opening"]


# ── fragments: one pick per conversation via macro_choices ───────────────────


async def _setup_directed_conversation(client, cid: str) -> None:
    await dbmod.create_conversation(cid, "rm", "Bot", "a scenario")
    await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})
    await client.post(
        "/api/fragments",
        json={
            "id": "vivid",
            "label": "Vivid",
            "description": "Vivid palette.",
            "prompt_text": "Paint the scene {{random::crimson::azure}}.",
        },
    )


def _direct_scene(arguments: dict) -> list[dict]:
    return [{"type": "function", "function": {"name": "direct_scene", "arguments": arguments}}]


async def test_mood_fragment_random_fixed_per_conversation(client, db, llm_mock):
    cid = "conv-rm-mood"
    await _setup_directed_conversation(client, cid)

    llm_mock.enqueue_director(_direct_scene({"moods": ["vivid"]}))
    llm_mock.enqueue_writer("first")
    first_block = _injection_block(await _drain(handle_turn(cid, "hello")))
    assert "{{random" not in first_block
    pick = next(w for w in ("crimson", "azure") if w in first_block)

    state = await dbmod.get_director_state(cid)
    assert state["macro_choices"] == {"mood:vivid:{{random::crimson::azure}}:0": pick}

    # Second turn re-reads the committed map: byte-identical injection.
    llm_mock.enqueue_director(_direct_scene({"moods": ["vivid"]}))
    llm_mock.enqueue_writer("second")
    second_block = _injection_block(await _drain(handle_turn(cid, "again")))
    assert second_block == first_block

    state = await dbmod.get_director_state(cid)
    assert state["macro_choices"] == {"mood:vivid:{{random::crimson::azure}}:0": pick}


async def test_interactive_fragment_value_random_rolls_fresh(client, db, llm_mock):
    cid = "conv-rm-interactive"
    await _setup_directed_conversation(client, cid)
    await client.post(
        "/api/interactive-fragments",
        json={
            "id": "style",
            "label": "Style",
            "description": "Narration style.",
            "field_type": "string",
            "injection_label": "Style",
        },
    )

    # Director-authored values roll fresh per emission: resolved in the block,
    # no pick pinned in macro_choices (unlike fragment source text above).
    for turn_msg in ("hello", "again"):
        llm_mock.enqueue_director(_direct_scene({"moods": [], "style": "make it {{random::loud::quiet}}"}))
        llm_mock.enqueue_writer("ok")
        block = _injection_block(await _drain(handle_turn(cid, turn_msg)))
        assert "{{random" not in block
        assert any(f"make it {w}" in block for w in ("loud", "quiet"))

    state = await dbmod.get_director_state(cid)
    assert state["macro_choices"] == {}


async def test_checkpoint_copies_macro_choices(client, db, llm_mock):
    cid = "conv-rm-checkpoint"
    await _setup_directed_conversation(client, cid)
    llm_mock.enqueue_director(_direct_scene({"moods": ["vivid"]}))
    llm_mock.enqueue_writer("ok")
    await _drain(handle_turn(cid, "hello"))
    source = await dbmod.get_director_state(cid)
    assert source["macro_choices"]

    resp = await client.post(f"/api/conversations/{cid}/checkpoint", json={"title": "cp"})
    assert resp.status_code == 200
    new_cid = resp.json()["id"]

    copied = await dbmod.get_director_state(new_cid)
    assert copied["macro_choices"] == source["macro_choices"]

    # Seeded {{random}} (persona/scenario fields) must not re-roll under the
    # copy's new id: the copy pins the source's seed, transitively (a
    # checkpoint of a checkpoint keeps the original seed).
    copy = await dbmod.get_conversation(new_cid)
    assert copy is not None and copy["macro_seed"] == cid

    resp = await client.post(f"/api/conversations/{new_cid}/checkpoint", json={"title": "cp2"})
    assert resp.status_code == 200
    grandchild = await dbmod.get_conversation(resp.json()["id"])
    assert grandchild is not None and grandchild["macro_seed"] == cid
