"""
test_kv_cache_real_stack.py — KV-cache invariants through the REAL turn stack.

The unit-level alarm (tests/unit/test_kv_cache_invariants.py) feeds
``_run_pipeline`` a hand-built ``prefix`` list. That proves the passes don't
mutate the bottom of the stack — but it sits *above* the real client and
*below* the prefix builder, so two whole layers go untested:

  • prefix CONSTRUCTION — ``build_prefix`` + persona/scenario/example-dialogue
    assembly + the DB persistence round-trip that reconstructs the next turn's
    history. A reformat, trim, or non-append-only rebuild there busts the
    cross-turn cache and the unit test cannot see it (it fakes the next prefix).
  • the dynamic director schema rebuilt each turn from ``get_interactive_fragments()``
    — if that query's row order is unstable, the tools blob drifts turn-over-turn.

These tests drive the genuine ``POST /send`` path (HTTP → handle_turn →
build_prefix → _run_pipeline → persistence) twice and assert the cache
invariants on the EXACT messages/tools each pass handed to ``complete()``,
captured at the ``LLMClient`` boundary by ``FakeLLMClient.captured``.

OUT OF SCOPE (cannot be caught here): the fake replaces ``LLMClient.complete``
wholesale, so ``profile.apply(body)`` and httpx's wire serialization never run;
and the inference server's own chat-template rendering is unknowable locally
(provider ``usage`` is the only ground truth for that — see kv-cache.md §8).
"""

from __future__ import annotations

import json

from backend.database import get_messages
from backend.inference.kv_tracker import _serialize_messages

# ── A draft well over the length-guard ceiling so the editor pass always fires.
_LONG_DRAFT = "word " * 60

# Smallest valid PNG (1×1, transparent). The /send attachment validator
# base64-decodes this, so it must be real base64 of a real image.
_PNG_1X1_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


def _wire_tools(tools) -> str:
    """Serialize tools the way httpx puts them on the wire: insertion order
    preserved, no sort_keys (the tracker's sorted view would hide key-order
    drift). Mirrors backend.inference.client's ``client.stream(..., json=body)``."""
    return json.dumps(tools, separators=(",", ":"), ensure_ascii=False) if tools else ""


async def _configure_all_features(client) -> None:
    """Enable agent + every cache-relevant tool, with a tight length guard so
    director (two calls), writer, and editor all fire in one turn."""
    resp = await client.put(
        "/api/settings",
        json={
            "model_name": "writer-model",
            "enable_agent": True,
            "enabled_tools": {
                "direct_scene": True,
                "rewrite_user_prompt": True,  # → director makes TWO calls
                "editor_apply_patch": True,
            },
            "length_guard_enabled": True,
            "length_guard_max_words": 5,  # _LONG_DRAFT (60 words) always trips it
        },
    )
    assert resp.status_code == 200


async def _make_conversation(client) -> str:
    # Macros in the card text exercise the cached base's macro ``resolve`` hook;
    # they must resolve identically on every pass and every turn.
    card = await client.post(
        "/api/characters",
        json={
            "name": "Aria",
            "description": "{{char}} is an elf ranger who guards {{user}}.",
            "first_mes": "Greetings, {{user}}. The woods are restless tonight.",
            "scenario": "Deep woods at dusk.",
        },
    )
    assert card.status_code == 200
    conv = await client.post("/api/conversations", json={"character_card_id": card.json()["id"]})
    assert conv.status_code == 200
    return conv.json()["id"]


async def _send(client, cid: str, content: str, attachments: list | None = None) -> None:
    resp = await client.post(
        f"/api/conversations/{cid}/send",
        json={"content": content, "attachments": attachments or []},
    )
    assert resp.status_code == 200
    _ = resp.text  # drain the buffered SSE stream so the turn fully completes


def _enqueue_turn(llm_mock) -> None:
    llm_mock.enqueue_writer(_LONG_DRAFT)
    llm_mock.enqueue_editor(None)  # no tool call → editor loop stops after recording iter 0


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_within_turn_all_passes_share_prefix_and_tools_through_build_prefix(client, llm_mock):
    """One real turn: every pass (both director calls, writer, editor) must ship
    the byte-identical system+history prefix produced by the real ``build_prefix``
    and an identical tools blob; the editor must extend the writer's full prompt."""
    await _configure_all_features(client)
    cid = await _make_conversation(client)
    _enqueue_turn(llm_mock)
    await _send(client, cid, "I draw my sword.")

    calls = llm_mock.captured
    assert [c["pass"] for c in calls].count("director") == 2, "expected two director calls (rewrite + direct_scene)"
    writer = next(c for c in calls if c["pass"] == "writer")
    editor = next(c for c in calls if c["pass"] == "editor")

    # The writer appends exactly one trailing message, so everything before it
    # is the shared prefix the cache depends on.
    prefix = writer["messages"][:-1]
    prefix_bytes = _serialize_messages(prefix)
    assert len(prefix) >= 1

    # Inv-1/2 — every pass starts with that identical system+history prefix.
    for c in calls:
        head = _serialize_messages(c["messages"][: len(prefix)])
        assert head == prefix_bytes, (
            f"CACHE BUST: pass {c['pass']!r} (tool_choice={c['tool_choice']}) does not start with the "
            "shared prefix — build_prefix or a pass rendered the system/history differently across passes."
        )

    # The base's macro ``resolve`` hook must scrub every {{char}}/{{user}} from
    # the bytes each pass actually shipped — including the card text carried in
    # the shared prefix. The recorded messages are post-resolution, so a raw
    # placeholder surviving here means the hook was dropped.
    for c in calls:
        sent = _serialize_messages(c["messages"])
        assert "{{char}}" not in sent and "{{user}}" not in sent, (
            f"MACRO LEAK: pass {c['pass']!r} shipped an unresolved placeholder to the model."
        )
        assert "Aria" in prefix_bytes  # {{char}} → the card name, resolved in the shared prefix

    # Inv-3 — wire-faithful tools blob identical across every pass, non-empty.
    blobs = {_wire_tools(c["tools"]) for c in calls}
    assert len(blobs) == 1, f"CACHE BUST: tools blob differs across passes; distinct sizes {sorted(len(b) for b in blobs)}"
    assert next(iter(blobs)), "expected a non-empty tools blob in single-model mode"

    # §3 — editor's prompt is a strict extension of the writer's full prompt.
    assert _serialize_messages(editor["messages"][: len(writer["messages"])]) == _serialize_messages(writer["messages"]), (
        "CACHE BUST: editor no longer extends the writer's prompt verbatim."
    )


async def test_cross_turn_prefix_is_append_only_through_persistence(client, llm_mock):
    """Two real turns: turn N+1's prefix, rebuilt from the DB, must be turn N's
    prefix plus exactly the persisted (user, assistant) pair — byte-for-byte —
    and the director's dynamic schema must be byte-stable across turns."""
    await _configure_all_features(client)
    cid = await _make_conversation(client)

    _enqueue_turn(llm_mock)
    await _send(client, cid, "hello there friend")
    n = len(llm_mock.captured)
    turn1 = list(llm_mock.captured)

    _enqueue_turn(llm_mock)
    await _send(client, cid, "and now I leave")
    turn2 = llm_mock.captured[n:]

    w1 = next(c for c in turn1 if c["pass"] == "writer")
    w2 = next(c for c in turn2 if c["pass"] == "writer")
    p1_bytes = _serialize_messages(w1["messages"][:-1])
    p2_bytes = _serialize_messages(w2["messages"][:-1])

    # §6 — the heart of cross-turn cache reuse: turn 2's bottom literally begins
    # with turn 1's bottom. This is what the unit test could only fake.
    assert p2_bytes.startswith(p1_bytes), (
        "CACHE BUST: turn 2's prefix is not an append-only extension of turn 1's. "
        "build_prefix or the persistence round-trip reformatted the carried-over history; "
        "every turn of a long session now re-bills from token zero."
    )
    assert len(p2_bytes) > len(p1_bytes)

    # The appended slice is exactly the (user, assistant) pair turn 1 persisted.
    remainder = p2_bytes[len(p1_bytes) :]
    assert "hello there friend" in remainder, "turn 1's user message is not carried into turn 2's prefix verbatim"
    assert "word" in remainder, "turn 1's assistant draft is not carried into turn 2's prefix verbatim"

    # Sanity: the DB really did persist the turn-1 exchange.
    roles = [m["role"] for m in await get_messages(cid)]
    assert roles[:3] == [
        "assistant",
        "user",
        "assistant",
    ], f"unexpected persisted history: {roles}"

    # Director's dynamic schema, rebuilt from get_interactive_fragments() each turn,
    # must be byte-identical across turns (this is the ONLY place a DB row-order
    # instability in the fragment query would show up).
    tools1 = {_wire_tools(c["tools"]) for c in turn1 if c["tools"]}
    tools2 = {_wire_tools(c["tools"]) for c in turn2 if c["tools"]}
    assert tools1 == tools2 and len(tools1) == 1, (
        "CACHE BUST: the tools blob is not byte-stable across turns — the dynamic "
        "director schema (or fragment row order) drifted, busting the tools region every turn."
    )


async def test_attachment_in_shared_history_is_byte_stable_across_passes_and_turns(client, llm_mock):
    """Invariant 2 — an image in the carried-over history must be encoded with
    the SAME bytes on every reference: identical across all passes of a turn,
    and surviving the DB round-trip into the next turn's cached prefix.

    Turn 1 sends the image (it rides the trailing pancake, the cheap top). Turn 2
    is plain text, so the image now lives in history — inside the cached prefix —
    where any per-pass re-encode or a lossy persistence round-trip would bust the
    cache."""
    await _configure_all_features(client)
    cid = await _make_conversation(client)

    _enqueue_turn(llm_mock)
    await _send(
        client,
        cid,
        "Look at this sketch.",
        attachments=[{"b64": _PNG_1X1_B64, "mime": "image/png", "filename": "sketch.png"}],
    )
    n = len(llm_mock.captured)
    turn1 = list(llm_mock.captured)

    _enqueue_turn(llm_mock)
    await _send(client, cid, "What do you make of it?")
    turn2 = llm_mock.captured[n:]

    # Within turn 2: every pass ships the byte-identical prefix — which now
    # contains the image. If any pass re-encoded the attachment, this rings.
    w2 = next(c for c in turn2 if c["pass"] == "writer")
    prefix2 = w2["messages"][:-1]
    prefix2_bytes = _serialize_messages(prefix2)
    for c in turn2:
        head = _serialize_messages(c["messages"][: len(prefix2)])
        assert head == prefix2_bytes, (
            f"CACHE BUST: pass {c['pass']!r} encoded the in-history image differently from the other passes."
        )

    # The image must actually be in the cached prefix as a data URL, proving the
    # persistence round-trip carried the exact bytes into history.
    assert f"data:image/png;base64,{_PNG_1X1_B64}" in prefix2_bytes, (
        "the attachment did not survive into turn 2's prefix as the same base64 — "
        "a lossy or re-encoding round-trip would bust the cache."
    )

    # Cross-turn append-only still holds with an attachment in the carried history.
    w1 = next(c for c in turn1 if c["pass"] == "writer")
    p1_bytes = _serialize_messages(w1["messages"][:-1])
    assert prefix2_bytes.startswith(p1_bytes), "CACHE BUST: image-bearing history broke append-only cross-turn growth."
