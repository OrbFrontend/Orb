"""Orchestrator-level coverage of the workflow pre/post-pipeline hooks.

Tests target the pre-pipeline iteration helper, the attachment staging helper, and a full ``run_pipeline`` run with patched LLM
passes to verify the post-pipeline draft-replacement and attachment-staging path.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from backend.database import (
    add_message,
    create_conversation,
    get_messages,
    get_workflow_message_state,
    set_active_leaf,
    set_workflow_config,
)
from backend.inference import KVCacheTracker, LLMClient
from backend.pipeline import handle_turn
from backend.pipeline.events import PROTECTED_TURN_EVENTS
from backend.pipeline.orchestrator import run_pipeline
from backend.pipeline.persistence import consume_pipeline
from backend.pipeline.workflow_bridge import (
    PostPipelineResult,
    _stage_workflow_attachment,
    iterate_pre_pipeline_hooks,
    run_post_pipeline,
)

from ._fixtures import make_workflow, register_for_test

_DIRECTOR_STATE = {"active_moods": []}
_PREFIX = [{"role": "system", "content": "You are an assistant."}]
_SETTINGS = {"model_name": "test", "enable_agent": 1, "enabled_tools": {}, "reasoning_enabled_passes": {}}
_REWRITER_CONFIG = {"variant_id": "test", "gpu": False, "batch_size": 1}
_ARTIFACT_HOOKS = {"produces_artifacts": True, "regenerate": lambda ctx, body: [], "reroll_gen": lambda ctx, params, seed: b""}


def _make_client() -> LLMClient:
    return LLMClient("http://localhost:9999")


async def _drain(gen) -> list:
    return [e async for e in gen]


def _writer(*deltas: str):
    async def writer(c, *args, **kwargs):
        for delta in deltas:
            yield {"type": "content", "delta": delta}

    return writer


def _pipeline_kwargs(enabled_tools: dict | None = None) -> dict:
    return {
        "prefix": _PREFIX,
        "enabled_tools": dict(enabled_tools or {}),
        "turn_scratch": {},
        "kv_tracker": KVCacheTracker(),
        "schema_overrides": {},
    }


async def _run_pre_hooks(
    accumulators: dict | None = None,
    *,
    enabled_tools: dict | None = None,
    last_user_message: str = "hi",
    turn_scratch: dict | None = None,
    client=None,
) -> list[dict]:
    return [
        event
        async for event in iterate_pre_pipeline_hooks(
            conversation_id="c1",
            history=[],
            last_user_message=last_user_message,
            settings=_SETTINGS,
            prefix_base=_PREFIX,
            enabled_tools_pre_merge=dict(enabled_tools or {}),
            turn_scratch=turn_scratch if turn_scratch is not None else {},
            client=client,
            kv_tracker=KVCacheTracker(),
            schema_overrides={},
            accumulators={"merged_enabled_tools": {}, "extras": []} if accumulators is None else accumulators,
        )
    ]


async def _run_post(draft: str = "draft", settings: dict | None = None, client=None, **kwargs) -> list:
    return await _drain(
        run_post_pipeline(
            draft=draft,
            conversation_id="c1",
            character_id=None,
            card=None,
            history=[],
            effective_msg="hi",
            director_output={},
            settings={"model_name": "test", **(settings or {})},
            prefix=_PREFIX,
            enabled_tools={},
            turn_scratch={},
            client=client or _make_client(),
            kv_tracker=KVCacheTracker(),
            schema_overrides={},
            **kwargs,
        )
    )


async def _run_with_writer(
    writer,
    *,
    client: LLMClient | None = None,
    last_user_message: str = "hello",
    history: list[dict] | None = None,
    pipeline_kwargs: dict | None = None,
) -> list[dict]:
    kwargs = {**_pipeline_kwargs(), **(pipeline_kwargs or {})}
    with patch("backend.pipeline.passes.writer.writer_pass", new=writer):
        return await _drain(
            run_pipeline(
                client or _make_client(), _SETTINGS, _DIRECTOR_STATE, [], [], last_user_message, history=history or [], **kwargs
            )
        )


async def _run_phase(phase: str, writer) -> list[dict]:
    return await _run_pre_hooks() if phase == "pre_pipeline" else await _run_with_writer(writer)


def _result(events: list[dict]) -> dict:
    [result] = [e for e in events if e["event"] == "_result"]
    return result["data"]


def _yielding(*events):
    async def hook(ctx):
        for event in events:
            yield event

    return hook


# -- iterate_pre_pipeline_hooks ------------------------------------------


@pytest.mark.parametrize(
    "hook_events,tools,merged,extras,forwarded",
    [
        ([], {"a": True}, {"a": True}, [], []),
        # Only true entries merge in; false entries are not added.
        (
            [{"type": "enable_tools", "tools": {"direct_scene": True, "editor_rewrite": False}}],
            {"editor_apply_patch": True},
            {"editor_apply_patch": True, "direct_scene": True},
            [],
            [],
        ),
        ([{"type": "enable_tools", "tools": {"direct_scene"}}], {}, {"direct_scene": True}, [], []),
        ([{"type": "enable_tools", "tools": {"not_a_real_tool": True}}], {}, {}, [], []),
        (
            [{"type": "system_prompt", "block": b} for b in ("   ", "", "real")],
            {},
            {},
            ["real"],
            [],
        ),
        (
            [{"event": "custom_sse", "data": {"hello": "world"}}],
            {},
            {},
            [],
            [{"event": "custom_sse", "data": {"hello": "world"}}],
        ),
    ],
)
async def test_pre_pipeline_iter_accumulates_hook_events(hook_events, tools, merged, extras, forwarded):
    accumulators = {"merged_enabled_tools": dict(tools), "extras": []}
    with register_for_test(make_workflow("tw_pre", pre_pipeline=_yielding(*hook_events))):
        events = await _run_pre_hooks(accumulators, enabled_tools=tools)
    assert events == forwarded
    assert accumulators["merged_enabled_tools"] == merged
    assert accumulators["extras"] == extras


async def test_pre_pipeline_iter_system_prompt_collected_in_subscription_order():
    # Equal priorities (both default 0) preserve registration order.
    w_a = make_workflow("w_a", pre_pipeline=_yielding({"type": "system_prompt", "block": "block-a"}))
    w_b = make_workflow("w_b", pre_pipeline=_yielding({"type": "system_prompt", "block": "block-b"}))
    accumulators = {"merged_enabled_tools": {}, "extras": []}
    with register_for_test(w_a), register_for_test(w_b):
        await _run_pre_hooks(accumulators)
    assert accumulators["extras"] == ["block-a", "block-b"]


_MALFORMED_EVENTS = [
    None,
    "not-an-event",
    [],
    {},
    {"data": {"x": 1}},
    {"event": ""},
    {"event": "bad\nname"},
    {"event": "bad_data", "data": []},
    {"event": "bad_data", "data": {"values": {1, 2}}},
]


@pytest.mark.parametrize("bad_event", _MALFORMED_EVENTS)
async def test_pre_pipeline_iter_drops_malformed_public_events(bad_event):
    with register_for_test(make_workflow("tw_bad_public", pre_pipeline=_yielding(bad_event))):
        assert await _run_pre_hooks() == []


@pytest.mark.parametrize("bad_event", _MALFORMED_EVENTS)
async def test_post_pipeline_iter_drops_malformed_public_events(bad_event):
    with register_for_test(make_workflow("tw_bad_post_public", post_pipeline=_yielding(bad_event))):
        events = await _run_post()
    assert len(events) == 1
    assert isinstance(events[0], PostPipelineResult)


def _patched_rewriter(rewrite, config=_REWRITER_CONFIG):
    return (
        patch("backend.workflows.prose_rewriter_host.resolve_config", return_value=config),
        patch("backend.workflows.prose_rewriter_host.rewrite_events", new=rewrite),
    )


async def test_prose_rewriter_runs_before_registered_post_pipeline_hooks(client):
    seen: list[str] = []

    async def prose_rewrite(source, config):
        assert source == "Editor-final draft."
        assert config["variant_id"] == "test"
        yield {"type": "draft_update", "draft": "Partial prose rewrite."}
        yield {"type": "rewritten", "draft": "Prose-rewritten draft."}

    async def post_hook(post_ctx):
        seen.append(post_ctx.draft)
        yield {"event": "downstream", "data": {"draft": post_ctx.draft}}

    resolve, rewrite = _patched_rewriter(prose_rewrite)
    with register_for_test(make_workflow("downstream", post_pipeline=post_hook)), resolve, rewrite:
        events = await _run_post("Editor-final draft.")

    assert seen == ["Prose-rewritten draft."]
    assert [event["event"] for event in events[:-1]] == [
        "phase_status",
        "draft_update",
        "writer_rewrite",
        "phase_status",
        "downstream",
    ]
    assert events[0]["data"] == {"channel": "workflow:prose_rewriter", "label": "Rewriting prose…"}
    assert events[3]["data"] == {"channel": "workflow:prose_rewriter", "state": "done"}
    assert isinstance(events[-1], PostPipelineResult)
    assert events[-1].draft == "Prose-rewritten draft."


@pytest.mark.parametrize(
    "workflow_settings",
    [
        {"workflow_enabled": {"prose_rewriter": False}},
        {"workflows_globally_enabled": 0},
        None,  # on, with automatic rewriting switched off
    ],
)
async def test_prose_rewriter_automatic_hook_obeys_workflow_enablement(client, workflow_settings):
    if workflow_settings is None:
        await set_workflow_config("prose_rewriter", {"automatic": False})
    # Asserted on the mock: the bridge isolates hook exceptions, so raising would pass too.
    with patch("backend.workflows.prose_rewriter_host.resolve_config", return_value=None) as resolve:
        events = await _run_post("Editor-final draft.", workflow_settings)
    resolve.assert_not_called()
    assert len(events) == 1
    assert isinstance(events[0], PostPipelineResult)
    assert events[0].draft == "Editor-final draft."


async def test_pre_pipeline_iter_hook_exception_logged_and_iteration_continues():
    survived = []

    async def crasher(pre_ctx):
        raise RuntimeError("boom")
        yield  # pragma: no cover -- generator shape

    async def hook_b(pre_ctx):
        survived.append("b_ran")
        yield {"type": "system_prompt", "block": "still here"}

    accumulators = {"merged_enabled_tools": {}, "extras": []}
    with (
        register_for_test(make_workflow("w_crash", pre_pipeline=crasher)),
        register_for_test(make_workflow("w_survive", pre_pipeline=hook_b)),
    ):
        await _run_pre_hooks(accumulators)
    assert survived == ["b_ran"]
    assert accumulators["extras"] == ["still here"]


# -- _stage_workflow_attachment ------------------------------------------


def _att(**overrides) -> dict:
    base = {"filename": "x.bin", "mime": "application/octet-stream", "data": b"x", "source": "workflow:tts"}
    return {**base, "workflow_id": "tts", **overrides}


def _path_att(path: str, **overrides) -> dict:
    return {key: value for key, value in _att(path=path, **overrides).items() if key != "data"}


async def test_stage_attachment_happy_path_with_data_bytes():
    staged = await _stage_workflow_attachment(_att(filename="out.mp3", mime="audio/mpeg", data=b"\xff\xfb"), "tts")
    assert staged is not None
    assert (staged["data"], staged["filename"], staged["source"]) == (b"\xff\xfb", "out.mp3", "workflow:tts")


@pytest.mark.parametrize(
    "attachment",
    [
        _att(source="workflow:other", workflow_id="other"),  # impersonation via source
        _att(workflow_id="other"),  # impersonation via workflow_id
        _att(path="/tmp/x"),  # both data and path
        {key: value for key, value in _att().items() if key != "data"},  # neither
        _att(data=b""),
        _path_att("/etc/passwd", filename="passwd", mime="text/plain"),  # outside the staging root
        _att(filename=123),
        _att(mime=None),
        "not a dict",
        None,
        ["list"],
    ],
)
async def test_stage_attachment_rejects(attachment):
    assert await _stage_workflow_attachment(attachment, "tts") is None


async def test_stage_attachment_path_is_normalized_to_bytes_or_dropped(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"on-disk-bytes")
    att = _path_att(str(p), filename="blob.bin")
    staged = await _stage_workflow_attachment(att, "tts")
    assert staged is not None
    assert "path" not in staged
    assert staged["data"] == b"on-disk-bytes"
    # A path that cannot be read drops the entry.
    assert await _stage_workflow_attachment({**att, "path": str(tmp_path / "ghost.bin")}, "tts") is None


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("annotation", "   \n  ", None),
        ("consumption_metadata", {"cues": [0.5, 1.0]}, {"cues": [0.5, 1.0]}),
        ("consumption_metadata", None, None),
        # A non-dict consumption_metadata coerces to None without rejecting the attachment.
        ("consumption_metadata", "string", None),
        ("consumption_metadata", 42, None),
        ("consumption_metadata", [1, 2, 3], None),
        ("consumption_metadata", True, None),
    ],
)
async def test_stage_attachment_normalizes_optional_fields(field, value, expected):
    staged = await _stage_workflow_attachment(_att(**{field: value}), "tts")
    assert staged is not None
    assert staged[field] == expected


# -- run_pipeline post-pipeline iteration --------------------------------


async def test_prose_rewriter_does_not_force_the_editor_to_run(client):
    async def prose_rewrite(source, _config):
        assert source == "Writer draft."
        yield {"type": "rewritten", "draft": "Prose-rewritten draft."}

    resolve, rewrite = _patched_rewriter(prose_rewrite)
    with resolve, rewrite:
        events = await _run_with_writer(_writer("Writer draft."))

    [writer_done] = [event for event in events if event["event"] == "writer_done"]
    assert writer_done["data"]["editor_will_run"] is False
    assert _result(events)["writer_draft"] == "Writer draft."
    assert _result(events)["resp_text"] == "Prose-rewritten draft."


async def test_run_pipeline_emits_single_result_with_staged_attachments():
    hook = _yielding(
        {"type": "attach_artifact", "attachment": _att(filename="tts.mp3", mime="audio/mpeg", data=b"mp3-bytes")},
        {"type": "attach_artifact", "attachment": _att(filename="transcript.txt", mime="text/plain", data=b"transcript")},
    )
    with register_for_test(make_workflow("tts", post_pipeline=hook, **_ARTIFACT_HOOKS)):
        payload = _result(await _run_with_writer(_writer("draft ", "body.")))
    assert payload["resp_text"] == "draft body."
    assert [a["filename"] for a in payload["staged_attachments"]] == ["tts.mp3", "transcript.txt"]
    assert all(a["source"] == "workflow:tts" for a in payload["staged_attachments"])


async def test_run_pipeline_drops_attach_artifact_with_mismatched_source():
    hook = _yielding({"type": "attach_artifact", "attachment": _att(source="workflow:other", workflow_id="other")})
    with register_for_test(make_workflow("tts", post_pipeline=hook, **_ARTIFACT_HOOKS)):
        assert _result(await _run_with_writer(_writer("draft")))["staged_attachments"] == []


async def test_run_pipeline_draft_replaced_emits_writer_rewrite_and_updates_result():
    # A second draft_replaced from the same hook is logged + ignored.
    hook = _yielding({"type": "draft_replaced", "draft": "rewritten"}, {"type": "draft_replaced", "draft": "rewritten again"})
    with register_for_test(make_workflow("rewriter", post_pipeline=hook)):
        events = await _run_with_writer(_writer("original"))

    rewrites = [e for e in events if e["event"] == "writer_rewrite"]
    assert len(rewrites) == 1
    assert rewrites[0]["data"]["refined_text"] == "rewritten"
    assert _result(events)["resp_text"] == "rewritten"


async def test_run_pipeline_turn_scratch_ref_shared_pre_to_post():
    captured: dict = {}
    client = _make_client()

    async def pre_hook(pre_ctx):
        captured["pre_id"] = id(pre_ctx.turn_scratch)
        pre_ctx.turn_scratch["from_pre"] = "stash"
        return
        yield  # pragma: no cover -- generator shape

    async def post_hook(post_ctx):
        captured["post_id"] = id(post_ctx.turn_scratch)
        captured["post_value"] = post_ctx.turn_scratch.get("from_pre")
        return
        yield  # pragma: no cover -- generator shape

    with register_for_test(make_workflow("scratch", pre_pipeline=pre_hook, post_pipeline=post_hook)):
        turn_scratch: dict = {}
        accumulators = {"merged_enabled_tools": {}, "extras": []}
        await _run_pre_hooks(accumulators, turn_scratch=turn_scratch, client=client)
        await _run_with_writer(
            _writer("ok"),
            client=client,
            last_user_message="hi",
            pipeline_kwargs={"enabled_tools": accumulators["merged_enabled_tools"], "turn_scratch": turn_scratch},
        )

    assert captured["pre_id"] == captured["post_id"]
    assert captured["post_value"] == "stash"


async def test_run_pipeline_turn_scratch_fresh_across_turns():
    captured: list[int] = []
    client = _make_client()

    async def post_hook(post_ctx):
        captured.append(id(post_ctx.turn_scratch))
        return
        yield  # pragma: no cover -- generator shape

    with register_for_test(make_workflow("scratch_lifetime", post_pipeline=post_hook)):
        for _ in range(2):
            await _run_with_writer(_writer("ok"), client=client, last_user_message="hi")
    assert captured[0] != captured[1]


async def test_run_pipeline_empty_registry_emits_single_result_no_staged():
    """No workflow registered: still exactly one _result with no staged attachments (the load-bearing parity property)."""
    events = await _run_with_writer(_writer("plain draft"))
    assert _result(events)["resp_text"] == "plain draft"
    assert _result(events)["staged_attachments"] == []
    assert not any(e["event"] == "_refined_result" for e in events)


async def test_run_pipeline_post_hook_exception_logged_and_pipeline_completes():
    async def crasher(post_ctx):
        raise RuntimeError("post boom")
        yield  # pragma: no cover -- generator shape

    with register_for_test(make_workflow("crasher", post_pipeline=crasher)):
        assert _result(await _run_with_writer(_writer("draft"), last_user_message="hi"))["resp_text"] == "draft"


async def test_run_pipeline_writer_abort_emits_result_skips_post_pipeline():
    """A writer-pass abort still persists via a final _result, but no downstream hook ever sees the aborted turn."""
    post_ran = []

    async def mock_writer(c, *args, **kwargs):
        c.abort()
        yield {"type": "content", "delta": "partial"}

    async def post_hook(post_ctx):
        post_ran.append(True)
        return
        yield  # pragma: no cover -- generator shape

    with register_for_test(make_workflow("never_runs", post_pipeline=post_hook)):
        result = _result(await _run_with_writer(mock_writer, last_user_message="hi"))
    assert result["resp_text"] == "partial"
    assert result["staged_attachments"] == []
    assert post_ran == []


@pytest.mark.parametrize(
    "hooks,expected",
    [
        ({"ms": [{"from": "a"}]}, {"ms": {"from": "a"}}),
        ({"ms": ["not-a-dict"]}, {}),
        ({"wf_a": [{"from": "a"}], "wf_b": [{"from": "b"}]}, {"wf_a": {"from": "a"}, "wf_b": {"from": "b"}}),
    ],
)
async def test_run_pipeline_set_message_state_collected_per_workflow_and_not_forwarded(hooks, expected):
    workflows = [
        make_workflow(wid, post_pipeline=_yielding(*({"type": "set_message_state", "state": s} for s in states)))
        for wid, states in hooks.items()
    ]
    with ExitStack() as stack:
        for workflow in workflows:
            stack.enter_context(register_for_test(workflow))
        events = await _run_with_writer(_writer("draft"))
    assert _result(events)["staged_message_state"] == expected
    assert not any(e.get("type") == "set_message_state" or e.get("event") == "set_message_state" for e in events)


async def test_post_pipeline_ctx_carries_agent_execution_and_readonly_history():
    """A hook forcing an Agent call needs its client and model; in single-model mode the agent lane IS the writer lane."""
    captured = {}
    client = _make_client()

    async def post_hook(post_ctx):
        captured.update(
            agent_client=post_ctx.agent_client,
            writer_client=post_ctx.client,
            agent_model_name=post_ctx.agent_model_name,
            history=post_ctx.history,
        )
        yield {"event": "noop", "data": {}}

    with register_for_test(make_workflow("agent_lane", post_pipeline=post_hook)):
        await _run_with_writer(_writer("draft"), client=client, history=[{"role": "user", "content": "earlier"}])

    assert captured["agent_client"] is client
    assert captured["agent_client"] is captured["writer_client"]
    assert captured["agent_model_name"] == _SETTINGS["model_name"]
    history = captured["history"]
    assert [m["role"] for m in history] == ["user"]
    assert history[0]["content"] == "earlier"
    with pytest.raises(AttributeError):
        history.append({"role": "user"})
    with pytest.raises(TypeError):
        history[0]["content"] = "x"


@pytest.mark.parametrize("deltas", [("reply",), ()])
async def test_post_pipeline_set_message_state_persists_only_with_an_assistant_row(client, deltas):
    cid = f"cms_{len(deltas)}"
    await create_conversation(cid, "T", "X", "")
    user_id, _ = await add_message(cid, "user", "hi", 0)
    await set_active_leaf(cid, user_id)

    hook = _yielding({"type": "set_message_state", "state": {"k": 1}})
    with (
        register_for_test(make_workflow("ms_persist", post_pipeline=hook)),
        patch("backend.pipeline.passes.writer.writer_pass", new=_writer(*deltas)),
    ):
        pipeline = run_pipeline(
            _make_client(), _SETTINGS, _DIRECTOR_STATE, [], [], "hi", conversation_id=cid, **_pipeline_kwargs()
        )
        await _drain(consume_pipeline(pipeline, cid, _SETTINGS, user_id, 1))

    assistants = [m for m in await get_messages(cid) if m["role"] == "assistant"]
    if deltas:
        assert await get_workflow_message_state(assistants[-1]["id"], "ms_persist") == {"k": 1}
    else:
        assert assistants == []


async def test_stop_interrupts_the_running_hook_keeps_its_finished_artifact_and_starts_no_other():
    """A render hook hands over a finished artifact, then blocks. Stop tears that work down, keeps the artifact (reported as
    accepted while the hook still runs), drops what the hook would publish after the stop, and starts no later hook.
    """
    client = _make_client()
    rendering = asyncio.Event()
    log: list[str] = []

    async def render(post_ctx):
        yield {
            "type": "attach_artifact",
            "attachment": _att(workflow_id="tw_render", source="workflow:tw_render", filename="a.png", mime="image/png"),
        }
        try:
            rendering.set()
            await asyncio.Event().wait()  # an uncancellable remote render, as far as the hook knows
        finally:
            log.append("render torn down")
        yield {"event": "tts_autoplay", "data": {}}

    async def later(post_ctx):
        log.append("later hook started")
        yield {"event": "later", "data": {}}

    accepted: list[PostPipelineResult] = []
    with (
        register_for_test(make_workflow("tw_render", post_pipeline=render, priority=-10, **_ARTIFACT_HOOKS)),
        register_for_test(make_workflow("tw_later", post_pipeline=later, priority=10)),
    ):
        task = asyncio.create_task(_run_post(client=client, on_accepted=accepted.append))
        await asyncio.wait_for(rendering.wait(), 2)
        assert [att["filename"] for att in accepted[-1].staged_attachments] == ["a.png"]
        client.abort()
        events = await asyncio.wait_for(task, 2)

    assert log == ["render torn down"]
    assert not [e for e in events if isinstance(e, dict) and e.get("event") in ("tts_autoplay", "later")]
    assert isinstance(events[-1], PostPipelineResult)
    assert [att["filename"] for att in events[-1].staged_attachments] == ["a.png"]


@pytest.mark.parametrize("phase", ["pre_pipeline", "post_pipeline"])
@pytest.mark.parametrize("name", sorted(PROTECTED_TURN_EVENTS))
async def test_turn_hooks_drop_protected_events_and_continue(phase, name, caplog):
    hook = _yielding(
        {"event": name, "data": {"forged": True}}, {"event": "custom_after_rejection", "data": {"extension": [1, 2]}}
    )
    later = _yielding({"event": "later_hook", "data": "still runs"})
    with (
        register_for_test(make_workflow("tw_owner", priority=-20, **{phase: hook})),
        register_for_test(make_workflow("tw_later_owner", priority=20, **{phase: later})),
    ):
        events = await _run_phase(phase, _writer("real reply"))
    assert not any(e.get("data") == {"forged": True} for e in events)
    assert [e["event"] for e in events if e["event"] in {"custom_after_rejection", "later_hook"}] == [
        "custom_after_rejection",
        "later_hook",
    ]
    assert f"event {name!r} is protected by the turn host" in caplog.text
    assert "tw_owner" in caplog.text and phase in caplog.text


_SHARED_EVENTS = [
    {"event": "phase_status", "data": {"channel": "workflow:tw_shared", "label": "Working", "extra": [1]}},
    {"event": "phase_status", "data": {"channel": "workflow:tw_shared", "state": "done"}},
    {"event": "reasoning", "data": {"pass": "workflow:tw_shared", "delta": "thought", "feature": {"x": 1}}},
    {"event": "draft_update", "data": {"draft": "cosmetic", "feature": True}},
    {"event": "warning", "data": {"headline": "Optional work declined", "sentence": "reason", "status": 503, "extra": [2]}},
    {"event": "tts_autoplay", "data": {}},
    {"event": "custom_without_data"},
    {"event": "custom_text", "data": "text"},
]


@pytest.mark.parametrize("phase", ["pre_pipeline", "post_pipeline"])
async def test_turn_hooks_preserve_shared_and_custom_json(phase):
    with register_for_test(make_workflow("tw_shared", **{phase: _yielding(*_SHARED_EVENTS)})):
        events = await _run_phase(phase, _writer("authoritative"))
    if phase == "post_pipeline":
        assert _result(events)["resp_text"] == "authoritative"
    assert [e for e in events if e in _SHARED_EVENTS] == _SHARED_EVENTS


@pytest.mark.parametrize("phase", ["pre_pipeline", "post_pipeline"])
@pytest.mark.parametrize(
    "bad_event,reason",
    [
        ({"event": "phase_status", "data": {"label": "working"}}, "phase_status.channel"),
        ({"event": "phase_status", "data": {"channel": "workflow:x"}}, "requires label or state"),
        ({"event": "phase_status", "data": {"channel": "workflow:x", "state": 1}}, "phase_status.state"),
        ({"event": "reasoning", "data": {"pass": "writer", "delta": 3}}, "reasoning.delta"),
        ({"event": "draft_update", "data": {"draft": None}}, "draft_update.draft"),
        ({"event": "warning", "data": {"headline": 1}}, "warning.headline"),
        ({"event": "warning", "data": {"headline": "failed", "status": True}}, "warning.status"),
        ({"event": "warning", "data": {"headline": "failed", "body": []}}, "warning.body"),
    ],
)
async def test_turn_hooks_drop_invalid_shared_fields(phase, bad_event, reason, caplog):
    with register_for_test(make_workflow("tw_bad_shared", **{phase: _yielding(bad_event, {"event": "custom_survives"})})):
        events = await _run_phase(phase, _writer("reply"))
    assert bad_event not in events
    assert {"event": "custom_survives"} in events
    assert reason in caplog.text


@pytest.mark.parametrize("phase", ["pre_pipeline", "post_pipeline"])
async def test_premature_hook_done_cannot_precede_the_successful_save(client, phase):
    await create_conversation("hook_done", "T", "X", "")
    await client.put("/api/settings", json={"enable_agent": False})

    async def replies():
        return [m for m in await get_messages("hook_done") if m["role"] == "assistant"]

    seen = []
    with (
        register_for_test(make_workflow("tw_done", **{phase: _yielding({"event": "done"}, {"event": "custom_after_done"})})),
        patch("backend.pipeline.passes.writer.writer_pass", new=_writer("saved reply")),
    ):
        async for event in handle_turn("hook_done", "hi"):
            seen.append(event)
            if event["event"] == "custom_after_done":
                assert not await replies()
            if event["event"] == "done":
                assert [m["content"] for m in await replies()] == ["saved reply"]
    assert [e["event"] for e in seen].count("done") == 1
    assert seen[-1] == {"event": "done"}
    assert not any(e["event"].startswith("_") for e in seen)
