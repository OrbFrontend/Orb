"""Phase 2: a compiled package actually running inside a turn and an action.

The lifecycle suite proves a package installs. This one proves the *executable*
half: that a published hook reaches the pipeline through a generic adapter, that
its context lands in the trailing prompt rather than the cached prefix, that a
failure isolates to the extension that caused it, and that the staged effect
transaction commits all-or-nothing.

Every test drives the real routes and the real turn handlers. Nothing here
constructs an ``Invocation`` by hand -- the unit suite does that, and this one
exists to catch the seams between the interpreter and everything around it.
"""

from __future__ import annotations

import asyncio

import pytest

import backend.database as dbmod
from backend.features.extensions import execution, interpreter
from backend.features.extensions.adapters import _invoke, _StateAccess
from backend.features.extensions.contracts import Flow, OpContext
from backend.features.extensions.errors import FlowError
from backend.features.extensions.interpreter import HostServices, Invocation
from backend.features.extensions.limits import MAX_JSON_STRING_BYTES
from backend.features.extensions.runtime import current_state
from backend.pipeline import handle_turn
from tests.extension_packages import (
    RESET_INPUT_SCHEMA,  # noqa: F401 -- documents the action contract under test
    orbext,
    scene_meter_manifest,
    scene_meter_package,
)

from .conftest import catalog, install

SCORE_CALL = {
    "tool_calls": [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "extension_output", "arguments": {"tension": 72}},
        }
    ]
}
"""What the mock returns for the Scene Meter hook's ``model.structured`` step.

The forced tool name is host-owned (``extension_output``), which is why the
mock's ``workflow`` queue routes it: a package contributes the schema, never the
name, so no package string reaches the model's function-calling surface.
"""


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


async def _conversation(cid: str = "conv-ext") -> str:
    await dbmod.create_conversation(cid, "ext", "Bot", "a scenario")
    return cid


async def _install_scene_meter(client, **kwargs):
    await client.put("/api/settings", json={"enable_agent": False})
    return await install(client, scene_meter_package(), **kwargs)


# ── publishing ──────────────────────────────────────────────────────────────


async def test_a_granted_package_publishes_both_hooks(client):
    await _install_scene_meter(client)
    from backend.workflows.registry import current_snapshot

    workflow = current_snapshot().get("scene-meter")
    assert {s.hook_type.value for s in workflow.subscriptions} == {
        "pre_pipeline",
        "post_pipeline",
    }
    assert current_state().get("scene-meter").blocked == ()


async def test_a_flow_reaching_an_unimplemented_operation_is_blocked_not_published(
    client,
    monkeypatch,
):
    """A later phase's operation blocks its entry point, exactly like a missing grant.

    Publishing it and failing mid-flow would be worse than not publishing:
    the user would see an extension that works until the step that does not.

    ``UNIMPLEMENTED_OPS`` is empty as of Phase 4, so the gate is exercised
    against a synthetic entry rather than deleted with the last operation that
    populated it. The seam is what this test is about -- it exists for the next
    operation whose contract ships ahead of its runtime, and an untested
    mechanism with nothing in it is the one that quietly stops working.
    """
    monkeypatch.setattr(interpreter, "UNIMPLEMENTED_OPS", frozenset({"http.request"}))
    package = orbext(
        {
            "orb-extension.json": scene_meter_manifest(
                requires={"operations": ["http.request"], "components": []},
                permissions=[
                    {
                        "capability": "network.request",
                        "origin": "https://example.invalid",
                    }
                ],
                hooks={"post_pipeline": {"flow": "flows/fetch.json", "stage": "observe"}},
                actions={},
                views={},
                placements=[],
            ),
            "flows/fetch.json": {
                "flow_version": 1,
                "steps": [
                    {
                        "op": "http.request",
                        "method": "GET",
                        "url": "https://example.invalid/x",
                    }
                ],
            },
        }
    )
    await install(client, package)
    entry = current_state().get("scene-meter")
    assert "hook post_pipeline" in entry.blocked
    from backend.workflows.registry import current_snapshot

    assert current_snapshot().get("scene-meter").subscriptions == []


async def test_a_disabled_package_contributes_nothing_to_a_turn(client, db, llm_mock):
    await _install_scene_meter(client, enabled=False)
    cid = await _conversation("conv-ext-off")
    llm_mock.enqueue_writer("She nods.")

    await _drain(handle_turn(cid, "hello"))

    assert await dbmod.get_workflow_state(cid, "scene-meter") is None
    assert [p for p, _ in llm_mock.calls] == ["writer"]


# ── turn integration ────────────────────────────────────────────────────────


async def test_the_post_hook_commits_its_state_after_the_turn(client, db, llm_mock):
    await _install_scene_meter(client)
    cid = await _conversation("conv-ext-post")
    llm_mock.enqueue_writer("She nods.")
    llm_mock.enqueue_workflow(SCORE_CALL)

    await _drain(handle_turn(cid, "hello"))

    assert await dbmod.get_workflow_state(cid, "scene-meter") == {"tension": 72}


async def test_extension_context_rides_the_trailing_message_not_the_cached_prefix(client, db, llm_mock):
    """The KV-cache rule for dynamic extension context, asserted on the wire.

    The block has to appear in the turn's *last* message and nowhere in the
    system message. Putting it in the prefix would evict the conversation's
    cached history every turn the value changed -- which for a meter is every
    turn.
    """
    await _install_scene_meter(client)
    cid = await _conversation("conv-ext-ctx")
    # Seed the state the pre-hook reads, so its context.append fires this turn.
    await dbmod.set_workflow_state(cid, "scene-meter", {"tension": 40})
    llm_mock.enqueue_writer("She nods.")
    llm_mock.enqueue_workflow(SCORE_CALL)

    await _drain(handle_turn(cid, "hello"))

    writer_calls = [c for c in llm_mock.captured if c["pass"] == "writer"]
    assert writer_calls, "the writer pass did not run"
    call = writer_calls[-1]
    assert "Current scene tension is 40 out of 100." in str(call["messages"][-1]["content"])
    assert "Scene tension" not in str(call["messages"][0]["content"])


async def test_extension_context_also_reaches_the_director_trailing(client, db, llm_mock):
    """Both declared targets, not just the writer.

    The Director's copy rides its per-turn trailing request beside the lorebook
    and direction-note fences, ahead of the tool instruction -- so the tools
    blob and the cached prefix are untouched by it.
    """
    await _install_scene_meter(client)
    await client.put(
        "/api/settings",
        json={"enable_agent": True, "enabled_tools": {"direct_scene": True}},
    )
    cid = await _conversation("conv-ext-director")
    await dbmod.set_workflow_state(cid, "scene-meter", {"tension": 40})
    llm_mock.enqueue_director(
        [
            {
                "type": "function",
                "function": {"name": "direct_scene", "arguments": {"moods": []}},
            }
        ]
    )
    llm_mock.enqueue_writer("She nods.")
    llm_mock.enqueue_workflow(SCORE_CALL)

    await _drain(handle_turn(cid, "hello"))

    director_calls = [c for c in llm_mock.captured if c["pass"] == "director"]
    assert director_calls, "the director pass did not run"
    call = director_calls[-1]
    assert "Current scene tension is 40 out of 100." in str(call["messages"][-1]["content"])
    assert "Scene tension" not in str(call["messages"][0]["content"])
    # The extension changed no tool schema: the blob is still the pipeline's.
    assert [t["function"]["name"] for t in call["tools"]] == ["direct_scene"]


async def test_a_failing_hook_does_not_abort_the_turn(client, db, llm_mock):
    """One extension's failure discards only that invocation.

    The reply still persists, and no partial state write survives -- the two
    halves of "a bad hook never prevents the assistant turn from persisting".
    """
    await _install_scene_meter(client)
    cid = await _conversation("conv-ext-fail")
    llm_mock.enqueue_writer("She nods.")
    # Structured output the declared schema rejects: tension must be an integer.
    llm_mock.enqueue_workflow(
        {
            "tool_calls": [
                {
                    "id": "c",
                    "type": "function",
                    "function": {
                        "name": "extension_output",
                        "arguments": {"tension": "hot"},
                    },
                }
            ]
        }
    )

    events = await _drain(handle_turn(cid, "hello"))

    # The hook really ran and really failed -- without this the "no state" check
    # below would also pass for a hook that was never invoked at all.
    assert "workflow" in [p for p, _ in llm_mock.calls]
    assert any(e.get("event") == "done" for e in events)
    messages = await dbmod.get_messages(cid)
    assert messages[-1]["role"] == "assistant" and messages[-1]["content"] == "She nods."
    assert await dbmod.get_workflow_state(cid, "scene-meter") is None


async def test_a_hook_whose_grant_was_revoked_writes_nothing(client, db, llm_mock):
    await _install_scene_meter(client)
    cid = await _conversation("conv-ext-revoked")
    response = await client.put(
        "/api/extensions/scene-meter/permissions",
        json={
            "permissions": [
                {"capability": "context.read", "field": "draft"},
                {"capability": "model.call", "lane": "agent"},
            ]
        },
    )
    assert response.status_code == 200, response.text

    llm_mock.enqueue_writer("She nods.")
    await _drain(handle_turn(cid, "hello"))

    # The hook is unpublished entirely -- its derived requirements are no longer
    # covered -- so the turn never even reaches a permission check.
    assert await dbmod.get_workflow_state(cid, "scene-meter") is None
    assert "hook post_pipeline" in current_state().get("scene-meter").blocked


# ── actions ─────────────────────────────────────────────────────────────────


async def test_an_action_validates_its_input_commits_state_and_returns_effects(client, db):
    await _install_scene_meter(client)
    cid = await _conversation("conv-ext-action")

    response = await client.post(
        "/api/extensions/scene-meter/actions/reset",
        json={"conversation_id": cid, "input": {"tension": 250}},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # Clamped by the flow, not by the route: the package owns the arithmetic.
    assert body["data"] == {"tension": 100}
    assert body["effects"] == [
        {
            "resource": "extension.view",
            "extension_id": "scene-meter",
            "view": "inspector",
        }
    ]
    assert body["runtime_generation"] == current_state().generation
    assert await dbmod.get_workflow_state(cid, "scene-meter") == {"tension": 100}


async def test_an_action_input_failing_its_declared_schema_is_rejected(client, db):
    await _install_scene_meter(client)
    cid = await _conversation("conv-ext-badinput")

    response = await client.post(
        "/api/extensions/scene-meter/actions/reset",
        json={"conversation_id": cid, "input": {"tension": "very"}},
    )
    assert response.status_code == 400
    assert "does not match its declared schema" in response.json()["detail"]
    assert await dbmod.get_workflow_state(cid, "scene-meter") is None


async def test_an_undeclared_action_is_not_reachable(client):
    await _install_scene_meter(client)
    response = await client.post("/api/extensions/scene-meter/actions/../../settings", json={"input": {}})
    assert response.status_code in (404, 405)

    response = await client.post("/api/extensions/scene-meter/actions/nope", json={"input": {}})
    assert response.status_code == 404


async def test_a_disabled_extension_refuses_its_actions(client, db):
    await _install_scene_meter(client, enabled=False)
    cid = await _conversation("conv-ext-action-off")
    response = await client.post(
        "/api/extensions/scene-meter/actions/reset",
        json={"conversation_id": cid, "input": {"tension": 10}},
    )
    assert response.status_code == 409
    assert await dbmod.get_workflow_state(cid, "scene-meter") is None


async def test_revoking_a_grant_unpublishes_the_action_that_needed_it(client, db):
    await _install_scene_meter(client)
    cid = await _conversation("conv-ext-action-revoke")
    await client.put(
        "/api/extensions/scene-meter/permissions",
        json={"permissions": [{"capability": "state.read", "scope": "conversation"}]},
    )
    response = await client.post(
        "/api/extensions/scene-meter/actions/reset",
        json={"conversation_id": cid, "input": {"tension": 10}},
    )
    assert response.status_code == 403
    assert await dbmod.get_workflow_state(cid, "scene-meter") is None


async def test_an_action_on_a_missing_conversation_is_a_404(client, db):
    await _install_scene_meter(client)
    response = await client.post(
        "/api/extensions/scene-meter/actions/reset",
        json={"conversation_id": "nope", "input": {"tension": 10}},
    )
    assert response.status_code == 404


# ── catalog ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_catalog_still_lists_the_package_either_way(client, enabled):
    await _install_scene_meter(client, enabled=enabled)
    row = next(e for e in (await catalog(client))["extensions"] if e["id"] == "scene-meter")
    assert row["load_status"] == "available"
    assert row["enabled"] is enabled


# ── transaction and lifecycle race regressions ─────────────────────────────


def _flow(*steps) -> Flow:
    return Flow.model_validate({"flow_version": 1, "steps": list(steps)})


async def test_multi_scope_database_commit_rolls_back_if_a_later_scope_fails(client):
    cid = await _conversation("conv-ext-atomic")
    await dbmod.set_workflow_config("scene-meter", {"old": "config"})
    await dbmod.set_workflow_state(cid, "scene-meter", {"old": "conversation"})

    with pytest.raises(TypeError):
        await dbmod.commit_extension_state(
            "scene-meter",
            {
                "config": {"new": "config"},
                # Fails JSON encoding only after the config UPDATE has executed.
                "conversation": {"bad": object()},
            },
            conversation_id=cid,
            character_id=None,
        )

    assert await dbmod.get_workflow_config("scene-meter") == {"old": "config"}
    assert await dbmod.get_workflow_state(cid, "scene-meter") == {"old": "conversation"}


async def test_revocation_after_the_last_privileged_step_prevents_commit(client):
    cid = await _conversation("conv-ext-mid-revoke")
    await execution.allow_new_invocations("scene-meter")
    grants = {("state.write", "conversation")}
    access = _StateAccess("scene-meter", conversation_id=cid, character_id=None)
    invocation = Invocation(
        extension_id="scene-meter",
        context=OpContext.ACTION,
        host=HostServices(grants=lambda: frozenset(grants), read_state=access.read),
        scopes_in_scope=frozenset({"conversation"}),
    )
    stream = _invoke(
        flow=_flow(
            {
                "op": "state.set",
                "scope": "conversation",
                "path": "saved",
                "value": True,
            },
            {"op": "ui.status", "text": "paused"},
        ),
        invocation=invocation,
        access=access,
    )
    assert (await anext(stream))["type"] == "status"
    grants.clear()
    with pytest.raises(FlowError, match="permission state.write"):
        await anext(stream)
    assert await dbmod.get_workflow_state(cid, "scene-meter") is None


async def test_context_budget_failure_discards_the_same_invocations_state(client):
    cid = await _conversation("conv-ext-context-budget")
    await execution.allow_new_invocations("scene-meter")
    grants = {
        ("state.write", "conversation"),
        ("prompt.context.append", "writer"),
    }
    access = _StateAccess("scene-meter", conversation_id=cid, character_id=None)
    invocation = Invocation(
        extension_id="scene-meter",
        context=OpContext.PRE_PIPELINE,
        host=HostServices(
            grants=lambda: frozenset(grants),
            read_state=access.read,
            context_block_error=lambda _blocks: "context blocks exceed the writer turn budget",
        ),
        scopes_in_scope=frozenset({"conversation"}),
    )
    with pytest.raises(FlowError, match="context blocks exceed"):
        async for _ in _invoke(
            flow=_flow(
                {
                    "op": "state.set",
                    "scope": "conversation",
                    "path": "saved",
                    "value": True,
                },
                {
                    "op": "context.append",
                    "targets": ["writer"],
                    "label": "L",
                    "text": "context",
                },
            ),
            invocation=invocation,
            access=access,
        ):
            pass
    assert await dbmod.get_workflow_state(cid, "scene-meter") is None


async def test_all_action_effects_roll_back_when_the_last_database_write_fails(client):
    await _install_scene_meter(client)
    cid = await _conversation("conv-ext-whole-effect-atomic")
    message_id, _rejected = await dbmod.add_message(cid, "user", "hello", 0)
    access = _StateAccess("scene-meter", conversation_id=cid, character_id=None)

    async def owns_message(candidate: int) -> bool:
        row = await dbmod.get_message_by_id(candidate)
        return bool(row and row["conversation_id"] == cid)

    invocation = Invocation(
        extension_id="scene-meter",
        context=OpContext.ACTION,
        host=HostServices(
            grants=lambda: frozenset(
                {
                    ("state.write", "conversation"),
                    ("conversation.branch.activate", None),
                }
            ),
            read_state=access.read,
            owns_message=owns_message,
        ),
        metadata={"conversation_id": cid},
        scopes_in_scope=frozenset({"conversation"}),
    )
    compiled = current_state().get("scene-meter").compiled
    stream = _invoke(
        compiled=compiled,
        flow=_flow(
            {
                "op": "state.set",
                "scope": "conversation",
                "path": "saved",
                "value": True,
            },
            {"op": "conversation.branch.activate", "message_id": message_id},
            {"op": "ui.status", "text": "paused"},
        ),
        invocation=invocation,
        access=access,
    )
    assert (await anext(stream))["type"] == "status"

    # The target disappears after staging but before commit. State is written
    # earlier in the transaction than branch activation and must still roll back.
    async with dbmod.get_db() as db:
        await db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        await db.commit()
    with pytest.raises(FlowError, match="not part of this conversation"):
        await anext(stream)
    assert await dbmod.get_workflow_state(cid, "scene-meter") is None


async def test_post_hook_message_state_commits_with_the_assistant_row(client, db, llm_mock):
    package = orbext(
        {
            "orb-extension.json": scene_meter_manifest(
                requires={"operations": ["state.set"], "components": []},
                permissions=[{"capability": "state.write", "scope": "message"}],
                hooks={"post_pipeline": {"flow": "flows/message.json", "stage": "observe"}},
                actions={},
                views={},
                placements=[],
            ),
            "flows/message.json": {
                "flow_version": 1,
                "steps": [
                    {
                        "op": "state.set",
                        "scope": "message",
                        "path": "label",
                        "value": "kept",
                    }
                ],
            },
        }
    )
    await install(client, package)
    cid = await _conversation("conv-ext-message-state")
    llm_mock.enqueue_writer("She nods.")

    await _drain(handle_turn(cid, "hello"))

    messages = await dbmod.get_messages(cid)
    assistant_id = next(message["id"] for message in reversed(messages) if message["role"] == "assistant")
    assert await dbmod.get_workflow_message_state(assistant_id, "scene-meter") == {"label": "kept"}


async def test_dual_model_hook_uses_the_agent_endpoint(client, db, llm_mock):
    await _install_scene_meter(client)
    endpoint = await client.post("/api/endpoints", json={"url": "http://agent.local", "api_key": "agent-key"})
    assert endpoint.status_code == 200
    configured = await client.put(
        "/api/settings",
        json={
            "agent_same_as_writer": False,
            "agent_endpoint_id": endpoint.json()["id"],
        },
    )
    assert configured.status_code == 200
    cid = await _conversation("conv-ext-dual-agent")
    llm_mock.enqueue_writer("She nods.")
    llm_mock.enqueue_workflow(SCORE_CALL)

    await _drain(handle_turn(cid, "hello"))

    workflow_call = next(call for call in llm_mock.captured if call["pass"] == "workflow")
    writer_call = next(call for call in llm_mock.captured if call["pass"] == "writer")
    assert workflow_call["endpoint"] == "http://agent.local"
    assert workflow_call["endpoint"] != writer_call["endpoint"]


async def test_hook_effects_keep_the_turns_captured_generation(client, db, llm_mock):
    await _install_scene_meter(client)
    cid = await _conversation("conv-ext-generation")
    original_generation = current_state().generation
    llm_mock.enqueue_writer("She nods.")
    llm_mock.enqueue_workflow(SCORE_CALL)
    gate = llm_mock.gate("workflow")

    turn = asyncio.create_task(_drain(handle_turn(cid, "hello")))
    await gate.reached.wait()
    try:
        changed = await client.post("/api/extensions/scene-meter/enabled", json={"enabled": False})
        assert changed.status_code == 200
        assert current_state().generation > original_generation
    finally:
        gate.release.set()
    events = await turn

    effect = next(event for event in events if event.get("event") == "extension_effects")
    assert effect["data"]["runtime_generation"] == original_generation


async def test_action_response_keeps_the_generation_captured_with_its_flow(client, llm_mock):
    package = orbext(
        {
            "orb-extension.json": scene_meter_manifest(
                requires={
                    "operations": ["model.structured", "return", "ui.toast"],
                    "components": [],
                },
                permissions=[{"capability": "model.call", "lane": "agent"}],
                hooks={},
                actions={"score": {"flow": "flows/score.json", "label": "Score"}},
                views={},
                placements=[],
            ),
            "flows/score.json": {
                "flow_version": 1,
                "steps": [
                    {
                        "id": "score",
                        "op": "model.structured",
                        "lane": "agent",
                        "prompt": "Score",
                        "output_schema": {
                            "type": "object",
                            "properties": {"tension": {"type": "integer"}},
                            "required": ["tension"],
                            "additionalProperties": False,
                        },
                    },
                    {"op": "ui.toast", "text": "Scored", "tone": "success"},
                    {"op": "return", "value": {"$ref": "steps.score"}},
                ],
            },
        }
    )
    await install(client, package)
    original_generation = current_state().generation
    llm_mock.enqueue_workflow(SCORE_CALL)
    gate = llm_mock.gate("workflow")

    action = asyncio.create_task(client.post("/api/extensions/scene-meter/actions/score", json={"input": {}}))
    await gate.reached.wait()
    try:
        changed = await client.post("/api/extensions/scene-meter/enabled", json={"enabled": False})
        assert changed.status_code == 200
    finally:
        gate.release.set()
    response = await action

    assert response.status_code == 200, response.text
    assert response.json()["runtime_generation"] == original_generation
    assert response.json()["data"] == {"tension": 72}
    assert response.json()["toasts"] == [{"text": "Scored", "tone": "success"}]


async def test_purge_waits_for_active_invocations_before_deleting_state(client):
    await install(client, scene_meter_package())
    cid = await _conversation("conv-ext-purge-drain")
    await dbmod.set_workflow_state(cid, "scene-meter", {"saved": True})
    preview = await client.post("/api/extensions/scene-meter/purge-data", json={})
    assert preview.status_code == 200

    entered = asyncio.Event()
    release = asyncio.Event()

    async def active() -> None:
        async with execution.track_invocation("scene-meter"):
            entered.set()
            await release.wait()

    worker = asyncio.create_task(active())
    await entered.wait()
    purge = asyncio.create_task(
        client.post(
            "/api/extensions/scene-meter/purge-data",
            json={"token": preview.json()["token"]},
        )
    )
    await asyncio.sleep(0)
    assert not purge.done()
    release.set()
    await worker
    response = await purge
    assert response.status_code == 200, response.text
    assert await dbmod.get_workflow_state(cid, "scene-meter") is None


async def test_action_input_uses_runtime_json_string_bounds(client):
    package = orbext(
        {
            "orb-extension.json": scene_meter_manifest(
                requires={"operations": ["return"], "components": []},
                permissions=[],
                hooks={},
                actions={
                    "echo": {
                        "flow": "flows/echo.json",
                        "label": "Echo",
                        "input_schema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    }
                },
                views={},
                placements=[],
            ),
            "flows/echo.json": {
                "flow_version": 1,
                "steps": [{"op": "return", "value": {"$ref": "input.text"}}],
            },
        }
    )
    await install(client, package)
    response = await client.post(
        "/api/extensions/scene-meter/actions/echo",
        json={"input": {"text": "x" * (MAX_JSON_STRING_BYTES + 1)}},
    )
    assert response.status_code == 400
    assert "string over" in response.json()["detail"]
