"""
test_kv_cache_entry_points.py — KV-cache tools/prefix consistency across EVERY
message-generating entry point, not just ``/send``.

Why this file exists (the gap that shipped a real cache regression):

    ``test_kv_cache_real_stack.py`` drives only ``handle_turn`` (``POST /send``)
    and asserts the cache invariants *within one turn* — every pass compared
    against its sibling passes. That can never catch a defect in a DIFFERENT
    entry point, and it can never catch a single-call handler that diverges from
    the conversation's established cache: ``handle_magic_rewrite`` once issued a
    single LLM call, so "all my calls agree with each other" was trivially true
    even when that one call shipped ``tools=None`` and busted the whole provider
    prefix cache (cached_tokens -> 0). It now runs the full pipeline, but the
    cross-entry-point invariant below still guards every call it makes.

    The real bug: every normal pass ships the byte-identical tool-schema blob, so
    the inference server caches a prefix that *includes* the templated tools
    region. ``magic_rewrite`` sent no tools, diverging the prompt at that region
    (near the top of the wire format) and re-billing from token zero.

These tests drive each entry point through the genuine HTTP → handler →
build_prefix → complete() stack and assert, at the ``FakeLLMClient`` boundary,
that every entry point ships a tools blob and system prefix consistent with the
conversation's cached turns. ``handle_turn`` is covered by the baseline turn each
test establishes; the parametrized cases cover regenerate / super_regenerate /
fork_edit / magic_rewrite.

OUT OF SCOPE (same as the real-stack file): the fake replaces ``complete``
wholesale, so the server's chat-template rendering is unknowable locally —
provider ``usage`` is the only ground truth for that (see kv-cache.md §8). Here
the tool *blob* each call ships is the local proxy for "the templated tools
region is byte-stable across entry points."
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from backend.database import get_messages
from backend.inference.kv_tracker import _serialize_messages

# A draft well over the length-guard ceiling so the editor pass always fires on
# the pipeline-backed entry points (regenerate / super_regenerate / fork_edit).
_LONG_DRAFT = "word " * 60


def _wire_tools(tools) -> str:
    """Serialize tools the way httpx puts them on the wire: insertion order
    preserved, no sort_keys. Mirrors backend.inference.client's ``json=body``."""
    return json.dumps(tools, separators=(",", ":"), ensure_ascii=False) if tools else ""


async def _configure_all_features(client) -> None:
    """Enable agent + every cache-relevant tool, with a tight length guard so
    director, writer, and editor all fire and ship a non-empty tools blob."""
    resp = await client.put(
        "/api/settings",
        json={
            "model_name": "writer-model",
            "enable_agent": True,
            "enabled_tools": {
                "direct_scene": True,
                "editor_apply_patch": True,
            },
            "length_guard_enabled": True,
            "length_guard_max_words": 5,  # _LONG_DRAFT (60 words) always trips it
        },
    )
    assert resp.status_code == 200


async def _make_conversation(client) -> str:
    # Macros in the card text exercise the cached base's macro ``resolve`` hook,
    # which every entry point must apply identically.
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


def _enqueue_turn(llm_mock) -> None:
    """Queue one writer draft + an editor stop for a single pipeline turn."""
    llm_mock.enqueue_writer(_LONG_DRAFT)
    llm_mock.enqueue_editor(None)  # no tool call → editor loop stops after iter 0


async def _send(client, cid: str, content: str) -> None:
    resp = await client.post(
        f"/api/conversations/{cid}/send",
        json={"content": content, "attachments": []},
    )
    assert resp.status_code == 200
    _ = resp.text  # drain the buffered SSE stream so the turn fully completes


@dataclass
class _Baseline:
    cid: str
    user_id: int
    asst_id: int
    tools_blob: str  # wire-faithful tools blob the turn's writer shipped
    system_bytes: str  # serialized system message (msg[0]) of the turn's writer
    writer_model: str  # model the turn's writer ran on (overlaid from the active endpoint)


async def _baseline_turn(client, llm_mock) -> _Baseline:
    """Configure features, open a conversation, run one real turn, and return the
    cache fingerprint (tools blob + system prefix) the conversation now expects
    every other entry point to match."""
    await _configure_all_features(client)
    cid = await _make_conversation(client)
    start = len(llm_mock.captured)
    _enqueue_turn(llm_mock)
    await _send(client, cid, "I draw my sword.")

    turn = llm_mock.captured[start:]
    writer = next(c for c in turn if c["pass"] == "writer")
    blob = _wire_tools(writer["tools"])
    assert blob, "baseline turn writer shipped an empty tools blob — fixture is wrong"

    msgs = await get_messages(cid)
    user_id = next(m["id"] for m in reversed(msgs) if m["role"] == "user")
    asst_id = next(m["id"] for m in reversed(msgs) if m["role"] == "assistant")
    return _Baseline(
        cid=cid,
        user_id=user_id,
        asst_id=asst_id,
        tools_blob=blob,
        system_bytes=_serialize_messages(writer["messages"][:1]),
        writer_model=writer["model"],
    )


# ── Per-entry-point drivers: each enqueues its responses and hits its route ─────


async def _drive_regenerate(client, llm_mock, b: _Baseline) -> None:
    _enqueue_turn(llm_mock)
    resp = await client.post(f"/api/conversations/{b.cid}/messages/{b.asst_id}/regenerate", json={})
    assert resp.status_code == 200
    _ = resp.text


async def _drive_super_regenerate(client, llm_mock, b: _Baseline) -> None:
    _enqueue_turn(llm_mock)
    resp = await client.post(f"/api/conversations/{b.cid}/messages/{b.asst_id}/super_regenerate", json={})
    assert resp.status_code == 200
    _ = resp.text


async def _drive_fork_edit(client, llm_mock, b: _Baseline) -> None:
    _enqueue_turn(llm_mock)
    resp = await client.post(
        f"/api/conversations/{b.cid}/messages/{b.user_id}/fork-edit",
        json={"content": "I sheathe my sword instead."},
    )
    assert resp.status_code == 200
    _ = resp.text


async def _drive_magic_rewrite(client, llm_mock, b: _Baseline) -> None:
    _enqueue_turn(llm_mock)
    resp = await client.post(
        f"/api/conversations/{b.cid}/messages/{b.asst_id}/magic_rewrite",
        json={"direction": "make it darker"},
    )
    assert resp.status_code == 200
    _ = resp.text


_ENTRY_POINTS = [
    ("regenerate", _drive_regenerate),
    ("super_regenerate", _drive_super_regenerate),
    ("fork_edit", _drive_fork_edit),
    ("magic_rewrite", _drive_magic_rewrite),
]


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,driver", _ENTRY_POINTS, ids=[e[0] for e in _ENTRY_POINTS])
async def test_entry_point_tools_blob_and_prefix_match_the_turn(client, llm_mock, name, driver):
    """Every entry point that generates a message must, on every LLM call it
    issues, ship a NON-EMPTY tools blob (single-model), keep that blob
    byte-identical across its own calls, and start from the conversation's cached
    system prefix. This is the invariant ``magic_rewrite`` violated by sending
    ``tools=None`` — caught here because we compare against the turn's cache, not
    only the handler's own sibling calls."""
    b = await _baseline_turn(client, llm_mock)

    start = len(llm_mock.captured)
    await driver(client, llm_mock, b)
    calls = llm_mock.captured[start:]
    assert calls, f"{name} issued no LLM calls"

    blobs = {_wire_tools(c["tools"]) for c in calls}

    # The regression catcher: a single-model generation call with no tools busts
    # the templated tools region of the cached prefix.
    assert "" not in blobs, (
        f"CACHE BUST: entry point {name!r} shipped a call with an EMPTY tools blob in "
        "single-model mode — the templated tools region diverges from the cached prefix "
        "and the provider re-bills from token zero (this is the magic_rewrite tools=None bug)."
    )

    # Internal consistency: one blob for all of this handler's calls.
    assert len(blobs) == 1, (
        f"CACHE BUST: entry point {name!r} ships >1 distinct tools blob across its own "
        f"passes; distinct sizes {sorted(len(x) for x in blobs)}."
    )

    assert blobs == {b.tools_blob}, (
        f"CACHE BUST: entry point {name!r} ships a tools blob differing from the "
        "conversation's turns — its tools region will not reuse the cached prefix."
    )

    # Every call starts from the same cached system prefix the turn established.
    for c in calls:
        assert _serialize_messages(c["messages"][:1]) == b.system_bytes, (
            f"CACHE BUST: entry point {name!r} did not start from the conversation's "
            "cached system prefix — build_prefix rendered the system body differently."
        )
        sent = _serialize_messages(c["messages"])
        assert "{{char}}" not in sent and "{{user}}" not in sent, (
            f"MACRO LEAK: entry point {name!r} shipped an unresolved placeholder to the model."
        )


async def test_magic_rewrite_writer_call_ships_a_stable_blob(client, llm_mock):
    """The magic_rewrite writer call ships a non-empty, cache-stable tools blob
    with tool_choice='none', on the writer model.

    The rewrite runs the full pipeline (director, writer, editor); its writer
    call mirrors super_regenerate -- the turn's blob shipped purely to keep the
    templated tools region byte-stable, with the writer barred from invoking a
    tool. An empty blob here is the original ``tools=None`` cache bust."""
    b = await _baseline_turn(client, llm_mock)

    start = len(llm_mock.captured)
    await _drive_magic_rewrite(client, llm_mock, b)
    calls = llm_mock.captured[start:]

    writer = next(c for c in calls if c["pass"] == "writer")
    assert _wire_tools(writer["tools"]), "magic_rewrite's writer call shipped an empty tools blob (the tools=None bust)"
    assert writer["tool_choice"] == "none", "magic_rewrite's writer call must send tool_choice='none'"
    assert writer["model"] == b.writer_model, "magic_rewrite must run on the writer model"


async def test_magic_rewrite_drops_tools_in_dual_model(client, llm_mock):
    """Dual-model counterpart: when the agent runs on a separate server, the
    writer lane carries NO tools (Invariant 5) because its KV cache lives on a
    different server than the agent's tool-bearing passes. magic_rewrite is a
    writer-style call, so it must match the writer server's tool-less cache —
    sending the agent's blob here would bust the writer cache instead of helping."""
    # A separate endpoint auto-provisions writer+agent model configs; pointing
    # ``agent_endpoint_id`` at it (with agent_same_as_writer=False) puts the
    # director/editor on that server while the writer stays on the active one.
    ep = await client.post("/api/endpoints", json={"url": "http://agent.local", "api_key": "k"})
    assert ep.status_code == 200
    ep_id = ep.json()["id"]

    await _configure_all_features(client)
    resp = await client.put(
        "/api/settings",
        json={"agent_same_as_writer": False, "agent_endpoint_id": ep_id},
    )
    assert resp.status_code == 200

    cid = await _make_conversation(client)
    start = len(llm_mock.captured)
    _enqueue_turn(llm_mock)
    await _send(client, cid, "I draw my sword.")
    turn = llm_mock.captured[start:]

    # Sanity: the dual turn really did put a non-empty tool blob on the agent
    # passes — so an empty writer/rewrite blob below is a deliberate drop, not a
    # "no tools configured" false pass.
    assert any(_wire_tools(c["tools"]) for c in turn), "expected the agent passes to carry a tools blob"
    writer = next(c for c in turn if c["pass"] == "writer")
    assert _wire_tools(writer["tools"]) == "", "dual-model writer must drop tools (Invariant 5)"
    writer_model = writer["model"]

    asst_id = next(m["id"] for m in reversed(await get_messages(cid)) if m["role"] == "assistant")

    start = len(llm_mock.captured)
    _enqueue_turn(llm_mock)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{asst_id}/magic_rewrite",
        json={"direction": "make it darker"},
    )
    assert resp.status_code == 200
    _ = resp.text
    calls = llm_mock.captured[start:]

    writer = next(c for c in calls if c["pass"] == "writer")
    assert _wire_tools(writer["tools"]) == "", (
        "CACHE BUST: in dual-model magic_rewrite shipped tools to the writer server, whose "
        "cache is tool-less -- it must drop tools to match the writer lane (Invariant 5)."
    )
    assert writer["tool_choice"] is None, "with no tools there is nothing to constrain -- tool_choice must be omitted"
    assert writer["model"] == writer_model, "magic_rewrite must run on the same (writer) model as the dual-model turn's writer"
