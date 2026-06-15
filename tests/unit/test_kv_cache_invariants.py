"""
test_kv_cache_invariants.py — the alarm bell for KV-cache prefix reuse.

WHY THIS EXISTS
---------------
Orb's whole speed/cost story rests on one rule (see docs/architecture/kv-cache.md):
within a turn every pass sends a *byte-identical prefix* (system prompt + chat
history) and a *byte-identical tools blob*, and only the trailing message(s)
differ. If any pass mutates, reorders, or re-renders the shared bottom — or lets
the tools blob drift between passes — the inference server can no longer reuse
its KV cache. The prompt is silently re-billed from token zero. That costs the
user real money on every single turn, and nothing else in the suite would catch
it because the output is still *correct*, just expensive.

So this test is deliberately paranoid. It drives the REAL pipeline
(``_run_pipeline`` with the real director/writer/editor passes — nothing
mocked but the network) and then asserts the invariants on the EXACT bytes
that were handed to ``client.complete()``. Two independent witnesses are
checked and required to agree:

  1. ``CapturingClient`` records the literal ``messages``/``tools`` of every
     ``complete()`` call — the true wire payload.
  2. ``kv_tracker._entries`` records what each pass *claims* it sent (this is
     the data the user reads in the KV report to decide whether the cache
     held). We assert the tracker is honest by reconciling it against (1).

If either witness shows the shared bottom diverging across passes, or the two
witnesses disagree, the test fails loudly with the offending label.

The invariants asserted (numbered per the architecture doc §4):
  • Inv-1/2  every pass's prompt starts with the identical system+history prefix
  • Inv-3    the tools blob is byte-identical across passes that share a model
  • §3       the editor's prompt is a strict extension of the writer's prompt
             (single-model only — in dual-model the editor lives on another server)
  • Inv-5    in dual-model the writer drops tools entirely
  • §6       across turns the new prefix is "old prefix + one (user,assistant) pair"
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.kv_tracker import (
    CachedBase,
    _common_prefix_len,
    _KVCacheTracker,
    _serialize_messages,
    _serialize_tools,
)
from backend.llm_client import AbortToken
from backend.orchestrator import _run_pipeline
from backend.passes.editor.editor import editor_pass
from backend.tool_registry import (
    build_direct_scene_tool,
    build_feedback_tool,
    enabled_schemas,
)


def _wire_tools(tools: Any) -> str:
    """Serialize a tools list the way it actually hits the wire.

    httpx sends ``client.stream(..., json=body)`` via
    ``json.dumps(..., separators=(",", ":"), ensure_ascii=False)`` with NO
    ``sort_keys`` — so the bytes preserve dict insertion order. The tracker's
    ``_serialize_tools`` uses ``sort_keys=True``, which normalizes key order
    away and therefore CANNOT see key-order drift (e.g. a fragment iterated in
    a different order, or fixed props inserted before dynamic ones). For schema
    *stability* assertions we must compare what the server actually receives,
    not the tracker's order-insensitive view.
    """
    if not tools:
        return ""
    return json.dumps(tools, separators=(",", ":"), ensure_ascii=False)


# ── A long writer draft so the length guard fires and the editor pass runs.
_WRITER_DRAFT = (
    "Steel rings as the blade leaves its sheath and the whole courtyard seems to "
    "hold its breath while dust drifts down through the slanting afternoon light "
    "and somewhere far off a bell begins to toll its slow uneven warning."
)

_INTERACTIVE_FRAGMENTS = [
    {
        "id": "pacing",
        "field_type": "string",
        "description": "Pacing of the scene",
        "injection_label": "Pacing",
        "sort_order": 0,
        "required": False,
        "enabled": True,
    },
    {
        "id": "props",
        "field_type": "array",
        "description": "Salient props",
        "injection_label": "Props",
        "sort_order": 1,
        "required": False,
        "enabled": True,
    },
]


class CapturingClient:
    """Deterministic ``LLMClient`` stand-in that records the exact
    ``messages`` and ``tools`` of every ``complete()`` call.

    Dispatch mirrors the production ``tool_choice`` contract: a forced
    ``editor_*`` / ``direct_scene`` function name selects that pass; ``"none"``
    or ``None`` is the writer.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls: list[dict] = []
        # The turn's clients share one abort token, mirroring LLMClient.
        self.abort_token = AbortToken()
        # FIFO of editor tool-call messages to return, one per ReAct iteration.
        # Empty → the editor returns no tool call and the loop stops.
        self._editor_queue: list[dict] = []

    def enqueue_editor_patch(self, search: str, replace: str) -> None:
        """Queue an ``editor_apply_patch`` call removing one banned span, so the
        re-audit's issue count strictly drops and the ReAct loop advances."""
        self._editor_queue.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"p{len(self._editor_queue)}",
                        "type": "function",
                        "function": {
                            "name": "editor_apply_patch",
                            "arguments": json.dumps({"patches": [{"search": search, "replace": replace}]}),
                        },
                    }
                ],
            }
        )

    def enqueue_editor_rewrite(self, text: str) -> None:
        """Queue an ``editor_rewrite`` call returning *text* as the new draft.
        Used to drive the rewrite branch of the ReAct loop (length guard /
        structural rewrite), which is where the tool list used to be narrowed."""
        self._editor_queue.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"r{len(self._editor_queue)}",
                        "type": "function",
                        "function": {
                            "name": "editor_rewrite",
                            "arguments": json.dumps({"rewritten_text": text}),
                        },
                    }
                ],
            }
        )

    @property
    def is_aborted(self) -> bool:
        return self.abort_token.is_aborted

    def abort(self) -> None:
        self.abort_token.abort()

    def _label(self, tool_choice: Any) -> str:
        if tool_choice in (None, "none"):
            return "writer"
        if isinstance(tool_choice, dict):
            name = tool_choice.get("function", {}).get("name", "")
            if name in ("editor_apply_patch", "editor_rewrite"):
                return "editor"
            if name == "give_feedback":
                return "feedback"
            if name in ("direct_scene", "rewrite_user_prompt"):
                return f"director:{name}"
            return name or "editor"
        return "editor"  # "auto"

    async def complete(self, messages, model, tools=None, tool_choice=None, **params):
        label = self._label(tool_choice)
        # Snapshot via the SAME serializer the tracker uses, so the two
        # witnesses are directly comparable byte-for-byte.
        self.calls.append(
            {
                "label": label,
                "model": model,
                "msgs_serialized": _serialize_messages(messages),
                "tools_serialized": _serialize_tools(tools),
                "tools_chars": len(_serialize_tools(tools)),
                # Wire-faithful (insertion-order) bytes — see _wire_tools. This
                # is what catches key-order drift the tracker's sorted view hides.
                "tools_wire": _wire_tools(tools),
            }
        )

        if label == "writer":
            yield {"type": "content", "delta": _WRITER_DRAFT}
            yield {
                "type": "done",
                "message": {"role": "assistant", "content": _WRITER_DRAFT},
            }
            return

        if label == "editor":
            # Pop the next queued patch; empty queue → no tool call → loop stops.
            msg = self._editor_queue.pop(0) if self._editor_queue else {"role": "assistant", "content": "", "tool_calls": []}
            yield {"type": "done", "message": msg}
            return

        if label == "feedback":
            yield {
                "type": "done",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "f1",
                            "type": "function",
                            "function": {"name": "give_feedback", "arguments": '{"next_actions": "Ask her name."}'},
                        }
                    ],
                },
            }
            return

        # director:* — return a well-formed forced call so the parse path runs.
        name = label.split(":", 1)[1]
        args = '{"moods": ["tense"], "pacing": "urgent"}' if name == "direct_scene" else "{}"
        yield {
            "type": "done",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    }
                ],
            },
        }


def _make_prefix(system: str, n_pairs: int) -> list[dict]:
    prefix: list[dict] = [{"role": "system", "content": system}]
    for i in range(n_pairs):
        prefix.append({"role": "user", "content": f"user turn {i}"})
        prefix.append({"role": "assistant", "content": f"assistant turn {i}"})
    return prefix


def _base_settings(**overrides) -> dict:
    settings = {
        "model_name": "writer-model",
        "enable_agent": 1,
        "enabled_tools": {"direct_scene": True, "editor_apply_patch": True},
        "reasoning_enabled_passes": {},
        "length_guard_enabled": 1,
        "length_guard_max_words": 5,  # tiny, so _WRITER_DRAFT always trips the guard
        "length_guard_max_paragraphs": 1,
    }
    settings.update(overrides)
    return settings


async def _run_turn(
    *,
    prefix: list[dict],
    settings: dict,
    conversation_id: str,
    client: CapturingClient,
    agent_client: CapturingClient | None = None,
    agent_prefix: list[dict] | None = None,
    feedback_fragments: list[dict] | None = None,
) -> tuple[_KVCacheTracker, CapturingClient, CapturingClient | None]:
    tracker = _KVCacheTracker(conversation_id=conversation_id)
    director = {"active_moods": [], "progressive_fields": {}}
    enabled_tools = dict(settings["enabled_tools"])
    # Writer-only fragments shape direct_scene; feedback fragments are passed in
    # alongside them so _run_pipeline's split sees both. The caller mirrors what
    # _prepare_turn does in production: when feedback is enabled the give_feedback
    # schema rides the shared blob (schema_overrides) and its enable bit is set.
    feedback_fragments = feedback_fragments or []
    interactive_fragments = [*_INTERACTIVE_FRAGMENTS, *feedback_fragments]
    schema_overrides = {"direct_scene": build_direct_scene_tool(_INTERACTIVE_FRAGMENTS)}
    if bool(settings.get("feedback_enabled", 0)) and feedback_fragments:
        schema_overrides["give_feedback"] = build_feedback_tool(feedback_fragments)
        enabled_tools["give_feedback"] = True

    gen = _run_pipeline(
        client,
        settings,
        director,
        [],  # mood_fragments
        interactive_fragments,
        "I draw my sword.",
        phrase_bank=[],  # not None → audit_enabled path is live
        agent_client=agent_client,
        agent_prefix=agent_prefix,
        conversation_id=conversation_id,
        prefix=prefix,
        enabled_tools=enabled_tools,
        turn_scratch={},
        kv_tracker=tracker,
        schema_overrides=schema_overrides,
        history=prefix[1:],
    )
    async for _ in gen:
        pass
    return tracker, client, agent_client


# ── Reconciliation: the two witnesses must agree ──────────────────────────────


def _reconcile_tracker_with_client(tracker: _KVCacheTracker, *clients: CapturingClient) -> None:
    """Every tracker entry's recorded bytes must match a real ``complete()``
    call. This proves the KV report the user trusts is not lying about what
    went on the wire."""
    wire: dict[tuple[str, str], list[str]] = {}
    for c in clients:
        if c is None:
            continue
        for call in c.calls:
            wire.setdefault((call["label"], call["msgs_serialized"]), []).append(call["tools_serialized"])

    for e in tracker._entries:
        key = (e["label"], e["msgs_serialized"])
        assert key in wire, (
            f"KV tracker reported a call the wire never saw (label={e['label']!r}); "
            "the tracker is lying about the prefix — fix kv_tracker.record() call sites."
        )
        # tools recorded by the tracker must match what the client received.
        assert e["tools_serialized"] in wire[key], (
            f"KV tracker's tools blob for {e['label']!r} differs from the wire payload; "
            "record() is being passed a different schema list than complete()."
        )


def _wire_tools_by_label(*clients: CapturingClient) -> dict[str, set[str]]:
    """Map pass-label → set of distinct wire-faithful tools blobs seen for it."""
    out: dict[str, set[str]] = {}
    for c in clients:
        if c is None:
            continue
        for call in c.calls:
            out.setdefault(call["label"], set()).add(call["tools_wire"])
    return out


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_direct_scene_schema_is_deterministic_and_dynamic():
    """Case-1 root cause guard: the dynamic ``direct_scene`` schema must be
    byte-stable (insertion order included) for identical fragment input, and
    must actually carry the fragment-derived properties.

    If ``build_direct_scene_tool`` ever iterates a set/dict with unstable order,
    or moves the fixed props relative to the dynamic ones, the wire bytes drift
    turn-over-turn and the cross-turn cache busts — invisibly to the tracker's
    ``sort_keys`` view. Comparing wire-faithful bytes here makes that ring."""
    a = build_direct_scene_tool(_INTERACTIVE_FRAGMENTS)
    b = build_direct_scene_tool(list(_INTERACTIVE_FRAGMENTS))  # same content, fresh list
    assert _wire_tools([a]) == _wire_tools([b]), (
        "build_direct_scene_tool is not byte-stable for identical input — the "
        "director's tool schema will drift across turns and bust the cache."
    )
    props = a["function"]["parameters"]["properties"]
    assert "pacing" in props and "props" in props, (
        "dynamic fragment properties are missing — a static schema leaked in where "
        "the dynamic one was expected (the exact shape of the cross-pass bug)."
    )


async def test_single_model_prefix_and_tools_are_byte_identical_across_passes():
    """Single-model turn: director, writer and editor must all ship the same
    system+history prefix and the same tools blob, and the editor must extend
    the writer's full prompt."""
    prefix = _make_prefix("You are a vivid roleplay narrator.", n_pairs=4)
    tracker, client, _ = await _run_turn(
        prefix=prefix,
        settings=_base_settings(),
        conversation_id="conv-single",
        client=CapturingClient("writer-model"),
    )

    _reconcile_tracker_with_client(tracker, client)

    entries = {e["label"]: e for e in tracker._entries}
    assert "director:direct_scene" in entries, "director pass did not fire"
    assert "writer" in entries, "writer pass did not fire"
    assert "editor" in entries, "editor pass did not fire (length guard should have tripped)"

    prefix_bytes = _serialize_messages(prefix)

    # Inv-1/2 — every pass starts with the identical system+history prefix.
    for label, e in entries.items():
        assert e["msgs_serialized"].startswith(prefix_bytes), (
            f"CACHE BUST: pass {label!r} does not start with the shared prefix. "
            "Some pass mutated/reordered the system prompt or history. "
            f"common-prefix held only {_common_prefix_len(prefix_bytes, e['msgs_serialized'])}/"
            f"{len(prefix_bytes)} chars."
        )

    # Inv-3 — tools blob byte-identical across all three passes, and non-empty.
    # Compare WIRE-FAITHFUL bytes (insertion order preserved), not the tracker's
    # sort_keys view, so a key-order drift between passes also rings.
    wire = _wire_tools_by_label(client)
    all_blobs = {b for blobs in wire.values() for b in blobs}
    assert len(all_blobs) == 1, (
        "CACHE BUST: the tools blob differs across passes — the schema list is not "
        "threaded byte-identically (Director and Editor must share one tools blob; "
        "only tool_choice may vary). Distinct blob sizes: " + json.dumps(sorted(len(b) for b in all_blobs))
    )
    assert next(iter(all_blobs)), "expected a non-empty tools blob in single-model mode"

    # §3 — the editor's prompt is a strict extension of the writer's prompt:
    # the editor reuses the writer's trailing user pancake verbatim, so the
    # editor's cached bottom = writer's entire prompt.
    assert entries["editor"]["msgs_serialized"].startswith(entries["writer"]["msgs_serialized"]), (
        "CACHE BUST: the editor's prompt no longer extends the writer's prompt. "
        "The editor must reuse the writer's trailing user message verbatim; if the "
        "writer-content builder and the editor's writer_user_msg drift apart, the "
        "editor loses its biggest cache saving."
    )


_FEEDBACK_FRAGMENT = {
    "id": "next_actions",
    "field_type": "feedback",
    "description": "Suggested next actions for the player",
    "injection_label": "Next actions",
    "sort_order": 0,
    "required": False,
    "enabled": True,
}


async def test_feedback_step_reuses_shared_blob_no_cache_bust():
    """The post-writer feedback step must NOT diverge the tools blob: with feedback
    enabled, ``give_feedback`` rides the shared per-turn blob (Invariant 3) and the
    feedback call reuses the same cached base as the director/writer/editor. Its
    wire tools bytes must therefore equal every other pass's — no blob swap, no
    deliberate cache miss (the old TOFIX)."""
    prefix = _make_prefix("You are a vivid roleplay narrator.", n_pairs=4)
    tracker, client, _ = await _run_turn(
        prefix=prefix,
        settings=_base_settings(feedback_enabled=1),
        conversation_id="conv-feedback-kv",
        client=CapturingClient("writer-model"),
        feedback_fragments=[_FEEDBACK_FRAGMENT],
    )

    _reconcile_tracker_with_client(tracker, client)

    entries = {e["label"]: e for e in tracker._entries}
    assert "feedback" in entries, "feedback step did not fire (feedback_enabled + fragment present)"

    # Inv-3 with give_feedback present: ONE tools blob across director, writer,
    # editor AND feedback. The feedback call no longer swaps the blob.
    wire = _wire_tools_by_label(client)
    all_blobs = {b for blobs in wire.values() for b in blobs}
    assert len(all_blobs) == 1, (
        "CACHE BUST: the feedback step diverged the tools blob — it must reuse the "
        "shared cached base, not swap in a give_feedback-only blob. Distinct blob "
        "sizes: " + json.dumps(sorted(len(b) for b in all_blobs))
    )

    # The single shared blob actually carries give_feedback (rides the blob, not a swap).
    the_blob = next(iter(all_blobs))
    assert '"give_feedback"' in the_blob, "give_feedback schema is missing from the shared tools blob"

    # Explicit cross-pass equality: feedback's blob == writer's blob == editor's.
    assert wire["feedback"] == wire["writer"] == wire["editor"], (
        "feedback/writer/editor tools blobs differ — the feedback step is not reusing the frozen shared base."
    )

    # Message-stack guard — the half a tools-only check misses. The feedback call
    # must EXTEND the writer/editor stack, not fork off base.prefix with a fresh
    # single message: it replays writer_user_msg + reply before its request, so
    # the writer's full message stack is a prefix of feedback's. Forking (the old
    # behaviour) collapsed the provider cache to just the system+tools block.
    prefix_bytes = _serialize_messages(prefix)
    fb_msgs = entries["feedback"]["msgs_serialized"]
    writer_msgs = entries["writer"]["msgs_serialized"]
    assert len(writer_msgs) > len(prefix_bytes), "writer stack should include the current-turn user message"
    assert fb_msgs.startswith(writer_msgs), (
        "CACHE BUST: the feedback step forked the message stack instead of extending "
        "the writer's — it must replay writer_user_msg + reply so it reuses "
        "prefix + current-turn exchange, not just base.prefix. Shared only "
        f"{_common_prefix_len(writer_msgs, fb_msgs)}/{len(writer_msgs)}c with the writer."
    )


async def test_dual_model_feedback_rides_agent_lane_writer_stays_empty():
    """Dual-model with feedback on: Invariant 5 must hold — the writer drops all
    tools — while give_feedback rides only the agent lane, where the feedback
    step actually runs. The feedback call must reuse the agent base (same blob as
    the editor), never the empty writer base."""
    writer_prefix = _make_prefix("You are a narrator.", n_pairs=3)
    agent_prefix = _make_prefix("AGENT system prompt — distinct from the writer's.", n_pairs=3)

    settings = _base_settings(
        model_name="writer-model",
        agent_same_as_writer=False,
        agent_model_name="agent-model",
        feedback_enabled=1,
    )
    tracker, client, agent_client = await _run_turn(
        prefix=writer_prefix,
        settings=settings,
        conversation_id="conv-dual-feedback",
        client=CapturingClient("writer-model"),
        agent_client=CapturingClient("agent-model"),
        agent_prefix=agent_prefix,
        feedback_fragments=[_FEEDBACK_FRAGMENT],
    )

    _reconcile_tracker_with_client(tracker, client, agent_client)

    # Invariant 5 — the writer (its own server) ships no tools even with feedback on.
    writer_wire = _wire_tools_by_label(client)
    assert writer_wire.get("writer") == {""}, "Inv-5 broken: the writer's tools blob is non-empty in dual-model."

    # The feedback step ran on the AGENT client, not the writer client.
    assert "feedback" not in writer_wire, "feedback must not run on the writer lane in dual-model."
    agent_wire = _wire_tools_by_label(agent_client)
    assert "feedback" in agent_wire, "feedback step did not run on the agent lane."

    # give_feedback rides only the agent lane, and the feedback call reuses the
    # agent base verbatim (same blob as the editor — no swap, no cache miss).
    agent_blobs = {b for blobs in agent_wire.values() for b in blobs}
    assert len(agent_blobs) == 1, "agent-lane passes must share one tools blob (feedback included)."
    assert '"give_feedback"' in next(iter(agent_blobs)), "give_feedback missing from the agent-lane blob."
    assert agent_wire["feedback"] == agent_wire["editor"], "feedback diverged from the agent base."


@pytest.mark.parametrize("system_prompt", ["You are a narrator.", "ANOTHER totally different system body."])
async def test_dual_model_agent_passes_share_agent_prefix_and_writer_drops_tools(
    system_prompt,
):
    """Dual-model: director+editor run on the agent server and must share the
    agent prefix + a byte-identical tools blob; the writer runs on its own
    server and must send NO tools (Inv-5)."""
    writer_prefix = _make_prefix(system_prompt, n_pairs=3)
    agent_prefix = _make_prefix("AGENT system prompt — distinct from the writer's.", n_pairs=3)

    settings = _base_settings(
        model_name="writer-model",
        agent_same_as_writer=False,
        agent_model_name="agent-model",
    )
    tracker, client, agent_client = await _run_turn(
        prefix=writer_prefix,
        settings=settings,
        conversation_id="conv-dual",
        client=CapturingClient("writer-model"),
        agent_client=CapturingClient("agent-model"),
        agent_prefix=agent_prefix,
    )

    _reconcile_tracker_with_client(tracker, client, agent_client)
    entries = {e["label"]: e for e in tracker._entries}

    writer_prefix_bytes = _serialize_messages(writer_prefix)
    agent_prefix_bytes = _serialize_messages(agent_prefix)

    # Agent passes (director + editor) ride the agent prefix.
    for label in ("director:direct_scene", "editor"):
        assert entries[label]["msgs_serialized"].startswith(agent_prefix_bytes), (
            f"CACHE BUST: agent pass {label!r} does not start with the agent prefix."
        )

    # Writer rides its own prefix and ships NO tools (Inv-5).
    assert entries["writer"]["msgs_serialized"].startswith(writer_prefix_bytes)
    assert entries["writer"]["tools_chars"] == 0, (
        "CACHE WASTE: in dual-model the writer must drop tools entirely — its KV "
        "cache lives on a different server than the agent, so tool schemas would "
        "burn tokens with no caching benefit."
    )

    # Agent passes still share an identical tools blob with each other —
    # checked on wire-faithful bytes so key-order drift between them also rings.
    wire = _wire_tools_by_label(client, agent_client)
    director_blobs = wire["director:direct_scene"]
    editor_blobs = wire["editor"]
    assert director_blobs == editor_blobs and len(director_blobs) == 1, (
        "CACHE BUST: Director and Editor (both on the agent server) no longer share a byte-identical tools blob."
    )
    assert next(iter(editor_blobs)), "agent passes should carry a non-empty tools blob"
    # Writer (other server) genuinely sends no tools.
    assert wire["writer"] == {""}


async def test_cross_turn_prefix_grows_by_exactly_one_pair():
    """§6 — turn N+1's prefix is turn N's prefix plus one (user,assistant)
    pair, byte-for-byte, so the cache flows turn-over-turn."""
    conversation_id = "conv-crossturn"
    p1 = _make_prefix("You are a vivid roleplay narrator.", n_pairs=2)

    tracker1, client1, _ = await _run_turn(
        prefix=p1,
        settings=_base_settings(),
        conversation_id=conversation_id,
        client=CapturingClient("writer-model"),
    )
    e1 = {e["label"]: e for e in tracker1._entries}

    # The next turn appends the just-finished (user, assistant) exchange.
    p2 = p1 + [
        {"role": "user", "content": "I draw my sword."},
        {"role": "assistant", "content": _WRITER_DRAFT},
    ]
    tracker2, client2, _ = await _run_turn(
        prefix=p2,
        settings=_base_settings(),
        conversation_id=conversation_id,
        client=CapturingClient("writer-model"),
    )
    e2 = {e["label"]: e for e in tracker2._entries}

    p1_bytes = _serialize_messages(p1)
    p2_bytes = _serialize_messages(p2)

    # Monotone growth: turn 2's prefix literally begins with turn 1's prefix.
    assert p2_bytes.startswith(p1_bytes), "cross-turn prefix is not an append-only extension of the prior turn"

    # Turn 1's writer and turn 2's director both honour their own prefixes.
    assert e1["writer"]["msgs_serialized"].startswith(p1_bytes)
    assert e2["director:direct_scene"]["msgs_serialized"].startswith(p2_bytes), (
        "CACHE BUST: turn N+1's first call does not extend turn N's prefix; the "
        "cross-turn KV carry-over is broken and every long session re-bills from zero."
    )

    # Case-1 cross-turn guard: the dynamic director/editor tool schema is rebuilt
    # every turn. Identical config must yield byte-identical wire tools across
    # turns, or the cache busts at the tools boundary turn-over-turn. Compared on
    # wire-faithful bytes because the tracker's sort_keys view would hide key-order
    # drift — the most likely form of schema instability.
    w1 = _wire_tools_by_label(client1)
    w2 = _wire_tools_by_label(client2)
    for label in ("director:direct_scene", "editor"):
        assert w1[label] == w2[label] and len(w1[label]) == 1, (
            f"CACHE BUST: {label!r}'s tool schema is not byte-stable across turns "
            "(rebuilt unstably from identical config). Every turn re-bills the tools "
            f"region. turn1={sorted(map(len, w1[label]))} turn2={sorted(map(len, w2[label]))}"
        )


@pytest.mark.parametrize("reasoning_on", [False, True])
async def test_editor_react_iterations_preserve_cached_bottom(reasoning_on):
    """§7 — across editor ReAct iterations the cached bottom (system + history +
    the writer's user pancake) must never change; only the top moves.

    The earlier tests stop the editor after one iteration, so the multi-iteration
    message bookkeeping went untested. This drives a real ≥2-iteration loop: each
    queued patch removes one banned-phrase occurrence, so the audit count strictly
    drops and the loop advances. Both message-feedback modes are covered — the
    default flat mode (``reasoning_on=False``, which rewrites the top two pancakes
    in place) and the append mode (``reasoning_on=True``)."""
    prefix = _make_prefix("You are the editor's bench.", n_pairs=3)
    writer_user = "<lorebook>\n**Scene Direction**: tense\n\nI strike the anvil."
    # Four occurrences → the count can strictly decrease across ≥2 iterations.
    draft = (
        "The shiver ran down her spine. "
        "Later the shiver ran down her spine. "
        "Again the shiver ran down her spine. "
        "Once more the shiver ran down her spine."
    )
    phrase_bank = [["shiver ran down her spine"]]

    client = CapturingClient("editor-model")
    for lead in ("The", "Later the", "Again the"):
        client.enqueue_editor_patch(f"{lead} shiver ran down her spine.", f"{lead} hall stayed silent.")

    settings = {"model_name": "editor-model", "editor_audit_toggles": None}
    base = CachedBase(
        prefix=tuple(prefix),
        tools=tuple(enabled_schemas({"editor_apply_patch": True}, {})),
        model="editor-model",
    )
    async for _ in editor_pass(
        client,
        base,
        "I strike the anvil.",
        draft,
        settings,
        phrase_bank,
        audit_enabled=True,
        length_guard=None,
        kv_tracker=None,
        reasoning_on=reasoning_on,
        audit_context_msgs=[],
        writer_user_msg=writer_user,
    ):
        pass

    editor_calls = [c for c in client.calls if c["label"] == "editor"]
    assert len(editor_calls) >= 2, f"expected ≥2 editor iterations, got {len(editor_calls)}"

    # _serialize_messages joins one JSON-escaped line per message (internal
    # newlines are escaped), so splitting on "\n" yields one line per message.
    bottom = _serialize_messages(prefix + [{"role": "user", "content": writer_user}])
    for i, c in enumerate(editor_calls):
        head = "\n".join(c["msgs_serialized"].split("\n")[: len(prefix) + 1])
        assert head == bottom, (
            f"CACHE BUST: editor iteration {i + 1} changed the cached bottom "
            "(system + history + the writer's user message) — the multi-thousand-token "
            "prefix is re-billed on every editor round."
        )

    # Inv-3 across iterations — the tools blob must stay byte-identical every
    # round. The schema list lives in the cached prefix; narrowing it mid-loop
    # would re-bill the tools region each iteration. (This particular path never
    # narrows, but the assertion documents the invariant cheaply; the rewrite
    # path is covered by test_editor_tools_blob_constant_across_tool_switch.)
    iter_blobs = {c["tools_wire"] for c in editor_calls}
    assert len(iter_blobs) == 1, "CACHE BUST: editor tools blob changed between iterations. " + json.dumps(
        sorted(len(b) for b in iter_blobs)
    )

    if reasoning_on:
        # Append-only: every iteration extends the previous prompt verbatim, so
        # even the writer's draft pancake stays cached (the doc §7 ideal).
        for a, b in zip(editor_calls, editor_calls[1:], strict=False):
            assert b["msgs_serialized"].startswith(a["msgs_serialized"]), (
                "CACHE BUST: reasoning-mode editor iteration is not append-only — "
                "it rebuilt the message list instead of appending the tool turn."
            )
    else:
        # Flat mode: the list length is constant; only the last two pancakes move.
        msg_counts = {c["msgs_serialized"].count("\n") for c in editor_calls}
        assert len(msg_counts) == 1, (
            "flat-mode editor changed its message count between iterations — it must "
            "rewrite the top two pancakes in place, not grow or rebuild the list."
        )


async def test_editor_tools_blob_constant_across_tool_switch():
    """Inv-3 regression: when the ReAct loop switches which tool it forces
    (rewrite on one iteration, patch on the next), the tools *blob* must NOT
    change — only ``tool_choice`` may. The loop used to narrow ``editor_tools``
    to a single-tool list when changing tools mid-flight, shrinking the schema
    blob (e.g. 3 tools → 1) and re-billing the tools region every iteration.

    This drives that exact path: a 3-tool enabled set, a length-guard-forced
    rewrite on iteration 1 whose result still carries a banned phrase, so the
    loop continues to iteration 2 now forcing ``editor_apply_patch``. Before the
    fix the two iterations shipped different-sized blobs; this asserts they don't.
    """
    prefix = _make_prefix("You are the editor's bench.", n_pairs=2)
    writer_user = "I strike the anvil."
    banned = "shiver ran down her spine"
    # Long enough to trip the (tiny) length guard, and banned-phrase-laden so the
    # initial audit has issues too.
    draft = " ".join([f"The {banned}."] * 6)
    phrase_bank = [[banned]]

    client = CapturingClient("editor-model")
    # Iteration 1 is force-rewrite (length guard). Return a rewrite that clears
    # the word limit but still repeats the banned phrase, so audit issues remain
    # and the loop advances to a patch-forced iteration 2 (queue then empty → stop).
    client.enqueue_editor_rewrite(" ".join([f"A {banned}."] * 4))

    settings = {"model_name": "editor-model", "editor_audit_toggles": None}
    length_guard = {"enforce": False, "max_words": 5, "max_paragraphs": 1}
    # 3-tool enabled set so a narrow-to-one would be visible as a byte change.
    base = CachedBase(
        prefix=tuple(prefix),
        tools=tuple(
            enabled_schemas(
                {
                    "direct_scene": True,
                    "editor_apply_patch": True,
                    "editor_rewrite": True,
                },
                {},
            )
        ),
        model="editor-model",
    )
    async for _ in editor_pass(
        client,
        base,
        writer_user,
        draft,
        settings,
        phrase_bank,
        audit_enabled=True,
        length_guard=length_guard,
        kv_tracker=None,
        reasoning_on=False,
        audit_context_msgs=[],
        writer_user_msg=writer_user,
    ):
        pass

    editor_calls = [c for c in client.calls if c["label"] == "editor"]
    assert len(editor_calls) >= 2, f"expected ≥2 editor iterations (rewrite then patch), got {len(editor_calls)}"

    # The forced tool changed across iterations — but the blob must be constant.
    blobs = {c["tools_wire"] for c in editor_calls}
    assert len(blobs) == 1, (
        "CACHE BUST: the editor narrowed its tools blob when switching the forced "
        "tool mid-loop. Tool selection must go through tool_choice alone; the "
        "schema list must stay byte-identical. Distinct blob sizes: " + json.dumps(sorted(len(b) for b in blobs))
    )
    # And it must genuinely be the full 3-tool set, not a coincidental match.
    full_blob = _wire_tools(
        enabled_schemas(
            {"direct_scene": True, "editor_apply_patch": True, "editor_rewrite": True},
            {},
        )
    )
    assert next(iter(blobs)) == full_blob, "editor shipped a tools blob that is not the full enabled set"
