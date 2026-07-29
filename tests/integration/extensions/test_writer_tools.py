"""Writer tools end to end: selection, the tail policy, the loop, the flow.

The unit suites cover the contract (what a v2 manifest may say) and the blob
(which schemas each lane receives). This one drives the real routes and the
real turn handler, because everything interesting about Writer tools lives in
the seams: a selection made in the manager has to reach a snapshot, a schema in
that snapshot has to reach the Writer's tool blob, a call the model returns has
to reach a compiled flow, and the flow's result has to reach the model's
continuation -- without any of it reaching persisted prose.
"""

from __future__ import annotations

import json

import pytest

import backend.database as dbmod
from backend.core import MAX_WRITER_TOOL_CALLS_PER_TURN, WRITER_TOOL_PREFIX
from backend.features.extensions import adapters, telemetry
from backend.features.extensions.runtime import current_state
from backend.pipeline import handle_regenerate, handle_turn
from backend.workflows.registry import current_snapshot
from tests.extension_packages import (
    OUTCOME_RESOLVER_ID,
    orbext,
    outcome_resolver_manifest,
    outcome_resolver_package,
    resolve_outcome_flow,
)

from .conftest import entry, install

WIRE_NAME = f"{WRITER_TOOL_PREFIX}{OUTCOME_RESOLVER_ID}--resolve_outcome"


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


async def _conversation(cid: str = "conv-wt") -> str:
    await dbmod.create_conversation(cid, "wt", "Bot", "a scenario")
    return cid


async def _install(client, *, package: bytes | None = None, enabled: bool = True, activate: bool = True):
    # Single-model keeps the assertions about *one* shared blob honest: the
    # dual-model split has its own tests, and mixing the two here would hide
    # which lane a schema actually landed in.
    await client.put("/api/settings", json={"enable_agent": False})
    await install(client, package or outcome_resolver_package(), enabled=enabled)
    if activate:
        response = await client.put(
            f"/api/extensions/{OUTCOME_RESOLVER_ID}/writer-tool-active",
            json={"active": True},
        )
        assert response.status_code == 200, response.text


def _call(arguments: dict, *, name: str = WIRE_NAME, call_id: str = "call-wt-1") -> list[dict]:
    return [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
    ]


def _writer_calls(llm_mock) -> list[dict]:
    return [c for c in llm_mock.captured if c["pass"] == "writer"]


# ── selection ───────────────────────────────────────────────────────────────


async def test_installing_publishes_the_binding_but_not_the_selection(client):
    """Availability is not activation. Both come from the same snapshot, and
    installing decides only the first."""
    await _install(client, activate=False)
    snapshot = current_snapshot()
    assert WIRE_NAME in snapshot.writer_tools
    assert snapshot.active_writer_tool is None


async def test_selecting_a_resolver_names_it_in_the_snapshot(client):
    await _install(client)
    assert current_snapshot().active_writer_tool == WIRE_NAME


async def test_selection_is_exclusive_and_swaps_transactionally(client):
    """Selecting B clears A. Not "should" -- the write clears every other row in
    the same transaction, so two packages cannot both claim the resolver even
    for an instant."""
    await _install(client)
    second = outcome_resolver_package(id="second-resolver", name="Second Resolver")
    await install(client, second)
    response = await client.put("/api/extensions/second-resolver/writer-tool-active", json={"active": True})
    assert response.status_code == 200

    assert current_snapshot().active_writer_tool == f"{WRITER_TOOL_PREFIX}second-resolver--resolve_outcome"
    assert (await entry(client, OUTCOME_RESOLVER_ID))["writer_tool"]["selected"] is False
    assert (await entry(client, "second-resolver"))["writer_tool"]["selected"] is True


async def test_a_stale_deselect_cannot_clear_another_packages_selection(client):
    await _install(client)
    await install(client, outcome_resolver_package(id="second-resolver", name="Second Resolver"))
    selected = await client.put("/api/extensions/second-resolver/writer-tool-active", json={"active": True})
    assert selected.status_code == 200

    # A stale tab still showing A as selected sends its old unchecked state after
    # B won. The path id scopes deactivation; it is not a global "clear".
    stale = await client.put(
        f"/api/extensions/{OUTCOME_RESOLVER_ID}/writer-tool-active",
        json={"active": False},
    )
    assert stale.status_code == 200
    assert current_snapshot().active_writer_tool == f"{WRITER_TOOL_PREFIX}second-resolver--resolve_outcome"
    assert (await entry(client, "second-resolver"))["writer_tool"]["selected"] is True


async def test_disabling_keeps_the_preference_but_stops_the_tool(client):
    """A retained selection is the point: re-enabling restores what the user
    chose, and disabling must not silently untick it."""
    await _install(client)
    await client.post(f"/api/extensions/{OUTCOME_RESOLVER_ID}/enabled", json={"enabled": False})

    assert current_snapshot().active_writer_tool is None
    row = (await entry(client, OUTCOME_RESOLVER_ID))["writer_tool"]
    assert row["selected"] is True
    assert row["active"] is False
    assert row["available"] is False

    await client.post(f"/api/extensions/{OUTCOME_RESOLVER_ID}/enabled", json={"enabled": True})
    assert current_snapshot().active_writer_tool == WIRE_NAME


async def test_revoking_the_grant_unpublishes_the_binding(client):
    await _install(client)
    await client.put(
        f"/api/extensions/{OUTCOME_RESOLVER_ID}/permissions",
        json={"permissions": [{"capability": "context.read", "field": "draft"}]},
    )
    assert "writer tool" in current_state().get(OUTCOME_RESOLVER_ID).blocked
    assert current_snapshot().writer_tools == {}
    assert current_snapshot().active_writer_tool is None


async def test_uninstall_removes_the_selection_with_the_row(client):
    """So a later package claiming the same id is not silently activated."""
    await _install(client)
    await client.delete(f"/api/extensions/{OUTCOME_RESOLVER_ID}")
    await install(client, outcome_resolver_package())
    assert current_snapshot().active_writer_tool is None


async def test_selecting_an_ineligible_package_is_refused(client):
    await _install(client, enabled=False, activate=False)
    response = await client.put(
        f"/api/extensions/{OUTCOME_RESOLVER_ID}/writer-tool-active",
        json={"active": True},
    )
    assert response.status_code == 400
    assert "enable the extension" in response.json()["detail"]


async def test_a_v1_package_offers_no_writer_tool_row(client):
    from tests.extension_packages import metadata_package

    await install(client, metadata_package())
    assert (await entry(client, "scene-meter"))["writer_tool"] is None


# ── the tool blob and the tail policy ───────────────────────────────────────


async def test_an_unselected_contribution_stays_out_of_the_blob(client, llm_mock):
    """Installed is not selected. An unselected package's schema text must not
    influence generation merely because the package exists."""
    await _install(client, activate=False)
    cid = await _conversation("conv-wt-unselected")
    llm_mock.enqueue_writer("She hesitates.")

    await _drain(handle_turn(cid, "go on"))

    writer = _writer_calls(llm_mock)[0]
    assert not any(WIRE_NAME == t["function"]["name"] for t in writer["tools"] or [])
    assert writer["tool_choice"] in (None, "none")


async def test_the_selected_schema_and_policy_reach_the_writer(client, llm_mock):
    await _install(client)
    cid = await _conversation("conv-wt-blob")
    llm_mock.enqueue_writer("She hesitates.")

    await _drain(handle_turn(cid, "go on"))

    writer = _writer_calls(llm_mock)[0]
    assert [t["function"]["name"] for t in writer["tools"]].count(WIRE_NAME) == 1
    assert writer["tool_choice"] == "auto"
    tail = writer["messages"][-1]["content"]
    assert f"call ONLY `{WIRE_NAME}`" in tail
    # The number the model reads is the constant the loop enforces, not a
    # second copy of it that can drift.
    assert f"up to {MAX_WRITER_TOOL_CALLS_PER_TURN} times this turn" in tail
    # Package-authored description and a host-derived parameter list, both
    # inside the fixed block rather than replacing any of it.
    assert "Resolve an uncertain action" in tail
    assert "action (required)" in tail


async def test_every_single_model_pass_shares_one_blob(client, llm_mock):
    """The union is byte-identical across passes, not merely equivalent."""
    await client.put("/api/settings", json={"enable_agent": True})
    await install(client, outcome_resolver_package())
    await client.put(f"/api/extensions/{OUTCOME_RESOLVER_ID}/writer-tool-active", json={"active": True})
    cid = await _conversation("conv-wt-parity")
    llm_mock.enqueue_director([])
    llm_mock.enqueue_writer("She hesitates.")

    await _drain(handle_turn(cid, "go on"))

    blobs = {json.dumps(c["tools"], sort_keys=True) for c in llm_mock.captured if c["tools"]}
    assert len(blobs) == 1
    assert WIRE_NAME in next(iter(blobs))


async def test_the_policy_is_the_last_content_part_with_attachments(client, llm_mock):
    """With images the block has to be a final *text part*, not part of a
    combined body the images then follow."""
    await _install(client)
    cid = await _conversation("conv-wt-multimodal")
    llm_mock.enqueue_writer("She hesitates.")

    await _drain(
        handle_turn(
            cid,
            "look",
            attachments=[{"mime_type": "image/png", "data_b64": "aGk="}],
        )
    )

    parts = _writer_calls(llm_mock)[0]["messages"][-1]["content"]
    assert isinstance(parts, list)
    assert parts[-1]["type"] == "text"
    assert f"call ONLY `{WIRE_NAME}`" in parts[-1]["text"]
    assert any(part["type"] == "image_url" for part in parts[:-1])


async def test_text_mode_takes_the_no_tools_path_with_a_diagnostic(client, llm_mock):
    """Native text completion does not render optional tools and only
    synthesizes a call when one schema is forced, so ``auto`` plus an
    instruction is not an optional-call protocol there."""
    await _install(client)
    endpoint = (await client.post("/api/endpoints", json={"url": "http://llama.local", "api_key": ""})).json()
    await client.put(f"/api/endpoints/{endpoint['id']}", json={"completion_mode": "text"})
    await client.put("/api/settings", json={"active_endpoint_id": endpoint["id"]})
    cid = await _conversation("conv-wt-text")
    llm_mock.enqueue_writer("She hesitates.")

    await _drain(handle_turn(cid, "go on"))

    writer = _writer_calls(llm_mock)[0]
    assert not any(t["function"]["name"] == WIRE_NAME for t in writer["tools"] or [])
    assert (await entry(client, OUTCOME_RESOLVER_ID))["writer_tool"]["compatible_with_writer_endpoint"] is False


# ── the writer loop ─────────────────────────────────────────────────────────


async def test_a_valid_call_runs_the_flow_and_the_writer_continues(client, llm_mock, db):
    """The issue #121 path: prose, pause, structured result, continuation."""
    telemetry.reset()
    await _install(client)
    arguments = {"action": "pick the lock", "difficulty": 12}
    cid = await _conversation("conv-wt-call")
    llm_mock.enqueue_writer("She reaches for the latch. ", tool_calls=_call(arguments))
    llm_mock.enqueue_writer("The tumblers give.")

    await _drain(handle_turn(cid, "pick the lock"))

    calls = _writer_calls(llm_mock)
    assert len(calls) == 2
    assert calls[0]["tool_choice"] == "auto"
    # Budget remains, so the continuation may still call. It returned prose
    # instead, which is what ended the loop.
    assert calls[1]["tool_choice"] == "auto"

    # The tool exchange is present in the *request*, and it is protocol-valid.
    tool_msg = next(m for m in calls[1]["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["status"] == "ok"
    assert payload["result"]["outcome"] in ("success", "failure")
    assert 1 <= payload["result"]["roll"] <= 20

    rows = await (await db.execute("SELECT content FROM messages WHERE role = 'assistant'")).fetchall()
    stored = rows[-1]["content"]
    assert stored == "She reaches for the latch. The tumblers give."
    # No tool syntax anywhere in what was persisted.
    assert WIRE_NAME not in stored and "tool_call" not in stored
    stats = telemetry.summary(OUTCOME_RESOLVER_ID)
    assert stats is not None
    assert stats["writer_tool_invocations"] == 1
    assert stats["last_writer_tool_input_bytes"] == len(
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    assert stats["last_writer_tool_output_bytes"] == len(
        json.dumps(payload["result"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


async def test_the_flow_receives_the_exact_prose_streamed_before_the_call(client, llm_mock):
    """``ctx.draft`` is host-supplied. The model never echoed it, and could not
    have redirected the invocation by trying."""
    await _install(client)
    cid = await _conversation("conv-wt-draft")
    llm_mock.enqueue_writer("She reaches for the latch. ", tool_calls=_call({"action": "pick", "difficulty": 3}))
    llm_mock.enqueue_writer("It opens.")

    await _drain(handle_turn(cid, "pick the lock"))

    state = await dbmod.get_workflow_state(cid, OUTCOME_RESOLVER_ID)
    assert state["draft_at_call"] == "She reaches for the latch. "


async def test_regeneration_gets_a_fresh_host_owned_attempt_seed(client, llm_mock, db, monkeypatch):
    await _install(client)
    captured_seeds: list[str] = []
    original_invoke = adapters._invoke

    async def capture_seed(**kwargs):
        captured_seeds.append(kwargs["invocation"].seed)
        async for event in original_invoke(**kwargs):
            yield event

    monkeypatch.setattr(adapters, "_invoke", capture_seed)
    cid = await _conversation("conv-wt-seed")
    for _ in range(2):
        llm_mock.enqueue_writer("A. ", tool_calls=_call({"action": "same", "difficulty": 10}))
        llm_mock.enqueue_writer("B.")

    await _drain(handle_turn(cid, "same input"))
    row = await (
        await db.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
            (cid,),
        )
    ).fetchone()
    assert row is not None
    await _drain(handle_regenerate(cid, row["id"]))

    assert len(captured_seeds) == 2
    assert captured_seeds[0] != captured_seeds[1]


@pytest.mark.parametrize(
    ("calls", "reason"),
    [
        (_call({"action": "x", "difficulty": 1}, name="direct_scene"), "an agent tool"),
        (_call({"action": "x", "difficulty": 1}, name=f"{WRITER_TOOL_PREFIX}other--nope"), "an unknown tool"),
        (_call({"action": "x"}), "arguments missing a required property"),
        (_call({"action": "x", "difficulty": 99}), "arguments outside the schema"),
    ],
)
async def test_a_rejected_call_executes_nothing_and_still_continues(client, llm_mock, calls, reason):
    await _install(client)
    cid = await _conversation(f"conv-wt-reject-{abs(hash(reason)) % 1000}")
    llm_mock.enqueue_writer("She tries. ", tool_calls=calls)
    llm_mock.enqueue_writer("Nothing happens.")

    await _drain(handle_turn(cid, "try"))

    # Nothing executed: the flow's only observable effect is its state write.
    assert await dbmod.get_workflow_state(cid, OUTCOME_RESOLVER_ID) is None
    # And the Writer still finished, from a continuation that saw the error.
    second = _writer_calls(llm_mock)[1]
    tool_msg = next(m for m in second["messages"] if m.get("role") == "tool")
    assert json.loads(tool_msg["content"])["status"] == "error"


async def test_the_writer_can_call_again_after_reacting_to_the_first_result(client, llm_mock):
    """The interleaved shape: roll, react in prose, roll again.

    The second call is the whole point of the budget -- the model chooses it
    *after* seeing the first result, which a single batched call cannot express.
    """
    await _install(client)
    cid = await _conversation("conv-wt-interleave")
    llm_mock.enqueue_writer("She reaches for the latch. ", tool_calls=_call({"action": "pick", "difficulty": 5}, call_id="c1"))
    llm_mock.enqueue_writer(
        "The lock gives. She tries the door. ", tool_calls=_call({"action": "shove", "difficulty": 9}, call_id="c2")
    )
    llm_mock.enqueue_writer("It swings open.")

    events = await _drain(handle_turn(cid, "pick the lock"))

    calls = _writer_calls(llm_mock)
    assert len(calls) == 3
    assert [c["tool_choice"] for c in calls] == ["auto", "auto", "auto"]

    # Both exchanges are in the final request, in order, each with its own id.
    final = calls[2]["messages"]
    assert [m["tool_call_id"] for m in final if m.get("role") == "tool"] == ["c1", "c2"]
    assert all(json.loads(m["content"])["status"] == "ok" for m in final if m.get("role") == "tool")

    # Each assistant message carries only its own segment. Accumulating into
    # every message would resend the whole draft once per call.
    assistant = [m["content"] for m in final if m.get("role") == "assistant"]
    assert assistant == ["She reaches for the latch. ", "The lock gives. She tries the door. "]

    # ``ctx.draft`` at the second call is everything streamed so far, not just
    # the segment that preceded it.
    state = await dbmod.get_workflow_state(cid, OUTCOME_RESOLVER_ID)
    assert state["draft_at_call"] == "She reaches for the latch. The lock gives. She tries the door. "
    assert [event["data"]["running"] for event in events if event["event"] == "writer_tool_status"] == [
        True,
        False,
        True,
        False,
    ]
    messages = await dbmod.get_messages(cid)
    assert messages[-1]["content"] == "She reaches for the latch. The lock gives. She tries the door. It swings open."


async def test_the_call_budget_is_a_host_property_not_a_provider_promise(client, llm_mock):
    """A model that keeps calling gets cut off by the loop, not asked nicely."""
    telemetry.reset()
    await _install(client)
    cid = await _conversation("conv-wt-budget")
    for n in range(MAX_WRITER_TOOL_CALLS_PER_TURN + 1):
        llm_mock.enqueue_writer(f"beat {n}. ", tool_calls=_call({"action": "try", "difficulty": 5}, call_id=f"c{n}"))
    llm_mock.enqueue_writer("never reached")

    await _drain(handle_turn(cid, "try"))

    calls = _writer_calls(llm_mock)
    # One completion per call, plus the final one that may no longer call.
    assert len(calls) == MAX_WRITER_TOOL_CALLS_PER_TURN + 1
    assert calls[-1]["tool_choice"] == "none"
    executed = [m for m in calls[-1]["messages"] if m.get("role") == "tool"]
    assert len(executed) == MAX_WRITER_TOOL_CALLS_PER_TURN
    # The over-budget call came back anyway and was dropped, not executed.
    stats = telemetry.summary(OUTCOME_RESOLVER_ID)
    assert stats["writer_tool_invocations"] == MAX_WRITER_TOOL_CALLS_PER_TURN


async def test_an_unavailable_resolver_is_not_retried(client, llm_mock):
    """A fixed unavailable result is non-retryable inside the same turn."""
    broken = orbext(
        {
            "orb-extension.json": outcome_resolver_manifest(),
            "flows/resolve-outcome.json": {
                "flow_version": 1,
                "steps": [{"op": "return", "value": {"outcome": "maybe", "roll": 3}}],
            },
        }
    )
    await _install(client, package=broken)
    cid = await _conversation("conv-wt-failbudget")
    llm_mock.enqueue_writer("try 0. ", tool_calls=_call({"action": "a", "difficulty": 5}, call_id="f0"))
    llm_mock.enqueue_writer("done")

    await _drain(handle_turn(cid, "try"))

    calls = _writer_calls(llm_mock)
    assert len(calls) == 2
    assert calls[-1]["tool_choice"] == "none"
    replies = [json.loads(m["content"]) for m in calls[-1]["messages"] if m.get("role") == "tool"]
    assert replies == [{"status": "error", "code": "resolver_unavailable"}]


async def test_provider_call_ids_do_not_define_roll_identity(client, llm_mock, monkeypatch):
    await _install(client)
    captured_seeds: list[str] = []
    original_invoke = adapters._invoke

    async def capture_seed(**kwargs):
        captured_seeds.append(kwargs["invocation"].seed)
        async for event in original_invoke(**kwargs):
            yield event

    monkeypatch.setattr(adapters, "_invoke", capture_seed)
    cid = await _conversation("conv-wt-reused-id")
    repeated = "provider-local-call-0"
    llm_mock.enqueue_writer("A. ", tool_calls=_call({"action": "first", "difficulty": 5}, call_id=repeated))
    llm_mock.enqueue_writer("B. ", tool_calls=_call({"action": "second", "difficulty": 5}, call_id=repeated))
    llm_mock.enqueue_writer("C.")

    await _drain(handle_turn(cid, "roll twice"))

    assert len(captured_seeds) == 2
    assert captured_seeds[0] != captured_seeds[1]
    assert "|0|" in captured_seeds[0]
    assert "|1|" in captured_seeds[1]


async def test_multiple_calls_in_one_message_execute_nothing(client, llm_mock):
    """Not a budget: "resolve two things" is not a request whose first half is
    meaningful, so neither runs."""
    await _install(client)
    cid = await _conversation("conv-wt-multi")
    both = [
        *_call({"action": "a", "difficulty": 5}, call_id="c1"),
        *_call({"action": "b", "difficulty": 5}, call_id="c2"),
    ]
    llm_mock.enqueue_writer("She tries. ", tool_calls=both)
    llm_mock.enqueue_writer("Nothing resolves.")

    await _drain(handle_turn(cid, "try"))

    assert await dbmod.get_workflow_state(cid, OUTCOME_RESOLVER_ID) is None
    replies = [m for m in _writer_calls(llm_mock)[1]["messages"] if m.get("role") == "tool"]
    # Every call still gets a reply, or the transcript would not be protocol-valid.
    assert len(replies) == 2
    assert all(json.loads(m["content"])["status"] == "error" for m in replies)


async def test_an_unusable_call_id_recovers_without_a_tool_exchange(client, llm_mock):
    """A tool result needs the provider's own id. Orb does not invent one."""
    await _install(client)
    cid = await _conversation("conv-wt-badid")
    llm_mock.enqueue_writer("She tries. ", tool_calls=_call({"action": "a", "difficulty": 5}, call_id="  "))
    llm_mock.enqueue_writer("Nothing resolves.")

    await _drain(handle_turn(cid, "try"))

    second = _writer_calls(llm_mock)[1]
    assert not any(m.get("role") == "tool" for m in second["messages"])
    assert not any("tool_calls" in m for m in second["messages"])
    assert "Continue the reply" in second["messages"][-1]["content"]


async def test_narrative_json_is_not_a_tool_call(client, llm_mock):
    """The generic content-body fallbacks are unsafe once prose has streamed:
    a character writing JSON must not become a call to an extension."""
    await _install(client)
    cid = await _conversation("conv-wt-prose")
    llm_mock.enqueue_writer(f'She reads the note: {{"name": "{WIRE_NAME}", "arguments": {{"action": "x"}}}}')

    await _drain(handle_turn(cid, "read"))

    assert len(_writer_calls(llm_mock)) == 1
    assert await dbmod.get_workflow_state(cid, OUTCOME_RESOLVER_ID) is None


async def test_a_failing_resolver_still_lets_the_writer_finish(client, llm_mock, db):
    """The Writer receives a fixed code, never internal text, and completes the
    reply rather than inventing a successful resolution."""
    broken = orbext(
        {
            "orb-extension.json": outcome_resolver_manifest(),
            # Returns a shape its own declared output schema rejects.
            "flows/resolve-outcome.json": {
                "flow_version": 1,
                "steps": [{"op": "return", "value": {"outcome": "maybe", "roll": 3}}],
            },
        }
    )
    await _install(client, package=broken)
    cid = await _conversation("conv-wt-fail")
    llm_mock.enqueue_writer("She tries. ", tool_calls=_call({"action": "a", "difficulty": 5}))
    llm_mock.enqueue_writer("The moment passes.")

    await _drain(handle_turn(cid, "try"))

    tool_msg = next(m for m in _writer_calls(llm_mock)[1]["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload == {"status": "error", "code": "resolver_unavailable"}

    rows = await (await db.execute("SELECT content FROM messages WHERE role = 'assistant'")).fetchall()
    assert rows[-1]["content"] == "She tries. The moment passes."


# ── downstream replay ───────────────────────────────────────────────────────


async def test_the_editor_replays_the_writer_transcript_in_single_model_mode(client, llm_mock):
    await install(client, outcome_resolver_package())
    await client.put(f"/api/extensions/{OUTCOME_RESOLVER_ID}/writer-tool-active", json={"active": True})
    # The length guard is the reliable way to force an editor call: the audit
    # path only runs the loop past a threshold, and a two-sentence fixture draft
    # is deliberately clean.
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "length_guard_enabled": 1,
            "length_guard_enforce": 1,
            "length_guard_max_words": 1,
        },
    )
    cid = await _conversation("conv-wt-editor")
    llm_mock.enqueue_director([])
    llm_mock.enqueue_writer("She reaches. ", tool_calls=_call({"action": "a", "difficulty": 5}))
    llm_mock.enqueue_writer("It gives.")
    llm_mock.enqueue_editor(None)

    await _drain(handle_turn(cid, "try"))

    editor = [c for c in llm_mock.captured if c["pass"] == "editor"]
    assert editor, "expected the length guard to force an editor call"
    messages = editor[0]["messages"]
    assert any(m.get("role") == "tool" for m in messages)
    # The canonical draft is stated explicitly, because the assistant message
    # immediately above holds only the post-tool continuation.
    assert "She reaches. It gives." in messages[-1]["content"]


async def test_a_dual_model_agent_never_sees_the_writer_tool(client, llm_mock):
    """Its base does not declare that tool, so a historical call to it would be
    a message that model was never told about."""
    endpoint = (await client.post("/api/endpoints", json={"url": "http://agent.local", "api_key": "k"})).json()
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "agent_same_as_writer": False, "agent_endpoint_id": endpoint["id"]},
    )
    await install(client, outcome_resolver_package())
    await client.put(f"/api/extensions/{OUTCOME_RESOLVER_ID}/writer-tool-active", json={"active": True})
    cid = await _conversation("conv-wt-dual")
    llm_mock.enqueue_director([])
    llm_mock.enqueue_writer("She reaches. ", tool_calls=_call({"action": "a", "difficulty": 5}))
    llm_mock.enqueue_writer("It gives.")

    await _drain(handle_turn(cid, "try"))

    for captured in llm_mock.captured:
        names = [t["function"]["name"] for t in captured["tools"] or []]
        if captured["pass"] == "writer":
            assert names == [WIRE_NAME]
        else:
            assert WIRE_NAME not in names
            assert not any(m.get("role") == "tool" for m in captured["messages"])


# ── the flow profile ────────────────────────────────────────────────────────


async def test_a_writer_tool_flow_cannot_write_message_state(client):
    """Refused at compile time, not at run time: there is no assistant row while
    the Writer is still writing it, so the target never exists."""
    package = orbext(
        {
            "orb-extension.json": outcome_resolver_manifest(
                requires={"operations": ["state.set", "return"], "components": []},
                permissions=[
                    {"capability": "writer.tool.contribute"},
                    {"capability": "state.write", "scope": "message"},
                ],
            ),
            "flows/resolve-outcome.json": {
                "flow_version": 1,
                "steps": [
                    {"op": "state.set", "scope": "message", "path": "x", "value": 1},
                    {"op": "return", "value": {"outcome": "success", "roll": 1}},
                ],
            },
        }
    )
    response = await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", package)})
    assert response.status_code == 400
    assert "scope 'message'" in response.json()["detail"]


@pytest.mark.parametrize(
    ("step", "fragment"),
    [
        ({"op": "draft.replace", "value": "x"}, "draft.replace"),
        ({"op": "context.append", "targets": ["writer"], "label": "L", "text": "x"}, "context.append"),
        ({"op": "ui.toast", "text": "x"}, "ui.toast"),
        ({"op": "card.tags.set", "tags": []}, "card.tags.set"),
        ({"op": "conversation.branch.activate", "message_id": 1}, "conversation.branch.activate"),
    ],
)
async def test_denied_operations_fail_compilation_in_the_writer_tool_context(client, step, fragment):
    package = orbext(
        {
            "orb-extension.json": outcome_resolver_manifest(
                requires={"operations": [step["op"], "return"], "components": []},
                permissions=[
                    {"capability": "writer.tool.contribute"},
                    {"capability": "draft.replace"},
                    {"capability": "prompt.context.append", "targets": ["writer"]},
                    {"capability": "card.write", "field": "tags"},
                    {"capability": "context.read", "field": "character"},
                    {"capability": "conversation.branch.activate"},
                    {"capability": "ui.contribute", "slot": "inspector"},
                ],
            ),
            "flows/resolve-outcome.json": {
                "flow_version": 1,
                "steps": [step, {"op": "return", "value": {"outcome": "success", "roll": 1}}],
            },
        }
    )
    response = await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", package)})
    assert response.status_code == 400
    assert fragment in response.json()["detail"]


async def test_the_state_write_commits_with_the_flow_not_the_turn(client, llm_mock):
    """Documented and tested: a resolver's namespaced write survives a Writer
    abort, exactly as its model and HTTP calls would have."""
    await _install(client)
    cid = await _conversation("conv-wt-abort")
    llm_mock.enqueue_writer("She reaches. ", tool_calls=_call({"action": "a", "difficulty": 5}))
    gate = llm_mock.gate("writer")  # the continuation
    gate.release.set()
    llm_mock.enqueue_writer("")

    await _drain(handle_turn(cid, "try"))

    assert (await dbmod.get_workflow_state(cid, OUTCOME_RESOLVER_ID))["last_roll"]


async def test_the_flow_uses_the_captured_revision_not_a_newer_one(client, llm_mock):
    """A turn captures one generation and cannot execute a tool from a revision
    other than the one whose schema it sent."""
    await _install(client)
    snapshot = current_snapshot()
    binding = snapshot.writer_tools[WIRE_NAME]
    assert binding.spec.content_digest == current_state().get(OUTCOME_RESOLVER_ID).digest

    response = await client.post(
        f"/api/extensions/{OUTCOME_RESOLVER_ID}/inspect-update",
        files={"file": ("pkg.orbext", outcome_resolver_package(version="2.0.0"))},
    )
    assert response.status_code == 200, response.text
    inspection = response.json()
    applied = await client.post(
        f"/api/extensions/{OUTCOME_RESOLVER_ID}/update",
        json={"token": inspection["token"], "permissions": [p["value"] for p in inspection["permissions"]]},
    )
    assert applied.status_code == 200, applied.text

    # The old snapshot still resolves the old binding; the new one is separate.
    assert snapshot.writer_tools[WIRE_NAME] is binding
    assert current_snapshot().writer_tools[WIRE_NAME] is not binding


def _unused() -> None:
    """Keeps the fixture import honest for readers grepping for the flow."""
    resolve_outcome_flow()
