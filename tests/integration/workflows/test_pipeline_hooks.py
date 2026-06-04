"""Orchestrator-level coverage of the workflow pre/post-pipeline hooks.

Tests target the pre-pipeline iteration helper, the attachment staging
helper, and a full ``_run_pipeline`` run with patched LLM passes to
verify the post-pipeline draft-replacement and attachment-staging path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.database import (
    add_message,
    create_conversation,
    get_messages,
    get_workflow_message_state,
    set_active_leaf,
)
from backend.kv_tracker import _KVCacheTracker
from backend.llm_client import LLMClient
from backend.orchestrator import (
    _consume_pipeline,
    _iterate_pre_pipeline_hooks,
    _run_pipeline,
    _stage_workflow_attachment,
)

from ._fixtures import make_workflow, register_for_test


_DIRECTOR_STATE = {"active_moods": []}
_PREFIX = [{"role": "system", "content": "You are an assistant."}]
_SETTINGS = {
    "model_name": "test",
    "enable_agent": 1,
    "enabled_tools": {},
    "reasoning_enabled_passes": {},
}


def _make_client() -> LLMClient:
    return LLMClient("http://localhost:9999")


async def _drain(gen) -> list[dict]:
    return [e async for e in gen]


def _pipeline_kwargs(enabled_tools: dict | None = None) -> dict:
    return {
        "prefix": _PREFIX,
        "enabled_tools": dict(enabled_tools or {}),
        "turn_scratch": {},
        "kv_tracker": _KVCacheTracker(),
        "schema_overrides": {},
    }


# -- _iterate_pre_pipeline_hooks ------------------------------------------


async def test_pre_pipeline_iter_empty_registry_no_events_no_accumulator_change():
    accumulators = {"merged_enabled_tools": {"a": True}, "extras": []}
    events = []
    async for ev in _iterate_pre_pipeline_hooks(
        conversation_id="c1",
        history=[],
        last_user_message="hello",
        settings={"model_name": "test"},
        prefix_base=_PREFIX,
        enabled_tools_pre_merge={"a": True},
        turn_scratch={},
        client=None,
        kv_tracker=_KVCacheTracker(),
        schema_overrides={},
        accumulators=accumulators,
    ):
        events.append(ev)
    assert events == []
    assert accumulators["merged_enabled_tools"] == {"a": True}
    assert accumulators["extras"] == []


async def test_pre_pipeline_iter_enable_tools_dict_merges_only_true_entries():
    async def hook(pre_ctx):
        yield {"type": "enable_tools", "tools": {"direct_scene": True, "rewrite_user_prompt": False}}

    w = make_workflow("tw_enable", pre_pipeline=hook)
    with register_for_test(w):
        accumulators = {"merged_enabled_tools": {"editor_apply_patch": True}, "extras": []}
        events = []
        async for ev in _iterate_pre_pipeline_hooks(
            conversation_id="c1",
            history=[],
            last_user_message="hi",
            settings={"model_name": "test"},
            prefix_base=_PREFIX,
            enabled_tools_pre_merge={"editor_apply_patch": True},
            turn_scratch={},
            client=None,
            kv_tracker=_KVCacheTracker(),
            schema_overrides={},
            accumulators=accumulators,
        ):
            events.append(ev)

    assert events == []
    assert accumulators["merged_enabled_tools"]["editor_apply_patch"] is True
    assert accumulators["merged_enabled_tools"]["direct_scene"] is True
    # False entries are not added.
    assert "rewrite_user_prompt" not in accumulators["merged_enabled_tools"]


async def test_pre_pipeline_iter_enable_tools_set_form_treats_each_as_true():
    async def hook(pre_ctx):
        yield {"type": "enable_tools", "tools": {"direct_scene"}}

    w = make_workflow("tw_set", pre_pipeline=hook)
    with register_for_test(w):
        accumulators = {"merged_enabled_tools": {}, "extras": []}
        async for _ in _iterate_pre_pipeline_hooks(
            conversation_id="c1",
            history=[],
            last_user_message="hi",
            settings={"model_name": "test"},
            prefix_base=_PREFIX,
            enabled_tools_pre_merge={},
            turn_scratch={},
            client=None,
            kv_tracker=_KVCacheTracker(),
            schema_overrides={},
            accumulators=accumulators,
        ):
            pass
    assert accumulators["merged_enabled_tools"] == {"direct_scene": True}


async def test_pre_pipeline_iter_unregistered_tool_name_dropped():
    async def hook(pre_ctx):
        yield {"type": "enable_tools", "tools": {"not_a_real_tool": True}}

    w = make_workflow("tw_drop", pre_pipeline=hook)
    with register_for_test(w):
        accumulators = {"merged_enabled_tools": {}, "extras": []}
        async for _ in _iterate_pre_pipeline_hooks(
            conversation_id="c1",
            history=[],
            last_user_message="hi",
            settings={"model_name": "test"},
            prefix_base=_PREFIX,
            enabled_tools_pre_merge={},
            turn_scratch={},
            client=None,
            kv_tracker=_KVCacheTracker(),
            schema_overrides={},
            accumulators=accumulators,
        ):
            pass
    assert accumulators["merged_enabled_tools"] == {}


async def test_pre_pipeline_iter_system_prompt_collected_in_subscription_order():
    async def hook_a(pre_ctx):
        yield {"type": "system_prompt", "block": "block-a"}

    async def hook_b(pre_ctx):
        yield {"type": "system_prompt", "block": "block-b"}

    # Equal priorities (both default 0) preserve registration order.
    w_a = make_workflow("w_a", pre_pipeline=hook_a)
    w_b = make_workflow("w_b", pre_pipeline=hook_b)
    with register_for_test(w_a), register_for_test(w_b):
        accumulators = {"merged_enabled_tools": {}, "extras": []}
        async for _ in _iterate_pre_pipeline_hooks(
            conversation_id="c1",
            history=[],
            last_user_message="hi",
            settings={"model_name": "test"},
            prefix_base=_PREFIX,
            enabled_tools_pre_merge={},
            turn_scratch={},
            client=None,
            kv_tracker=_KVCacheTracker(),
            schema_overrides={},
            accumulators=accumulators,
        ):
            pass
    assert accumulators["extras"] == ["block-a", "block-b"]


async def test_pre_pipeline_iter_empty_system_prompt_block_dropped():
    async def hook(pre_ctx):
        yield {"type": "system_prompt", "block": "   "}
        yield {"type": "system_prompt", "block": ""}
        yield {"type": "system_prompt", "block": "real"}

    w = make_workflow("tw_empty", pre_pipeline=hook)
    with register_for_test(w):
        accumulators = {"merged_enabled_tools": {}, "extras": []}
        async for _ in _iterate_pre_pipeline_hooks(
            conversation_id="c1",
            history=[],
            last_user_message="hi",
            settings={"model_name": "test"},
            prefix_base=_PREFIX,
            enabled_tools_pre_merge={},
            turn_scratch={},
            client=None,
            kv_tracker=_KVCacheTracker(),
            schema_overrides={},
            accumulators=accumulators,
        ):
            pass
    assert accumulators["extras"] == ["real"]


async def test_pre_pipeline_iter_passes_through_unknown_event_types():
    async def hook(pre_ctx):
        yield {"event": "custom_sse", "data": {"hello": "world"}}

    w = make_workflow("tw_pass", pre_pipeline=hook)
    with register_for_test(w):
        accumulators = {"merged_enabled_tools": {}, "extras": []}
        events = []
        async for ev in _iterate_pre_pipeline_hooks(
            conversation_id="c1",
            history=[],
            last_user_message="hi",
            settings={"model_name": "test"},
            prefix_base=_PREFIX,
            enabled_tools_pre_merge={},
            turn_scratch={},
            client=None,
            kv_tracker=_KVCacheTracker(),
            schema_overrides={},
            accumulators=accumulators,
        ):
            events.append(ev)
    assert events == [{"event": "custom_sse", "data": {"hello": "world"}}]


async def test_pre_pipeline_iter_hook_exception_logged_and_iteration_continues():
    survived = []

    async def crasher(pre_ctx):
        raise RuntimeError("boom")
        yield  # pragma: no cover -- generator shape

    async def hook_b(pre_ctx):
        survived.append("b_ran")
        yield {"type": "system_prompt", "block": "still here"}

    w_a = make_workflow("w_crash", pre_pipeline=crasher)
    w_b = make_workflow("w_survive", pre_pipeline=hook_b)
    with register_for_test(w_a), register_for_test(w_b):
        accumulators = {"merged_enabled_tools": {}, "extras": []}
        async for _ in _iterate_pre_pipeline_hooks(
            conversation_id="c1",
            history=[],
            last_user_message="hi",
            settings={"model_name": "test"},
            prefix_base=_PREFIX,
            enabled_tools_pre_merge={},
            turn_scratch={},
            client=None,
            kv_tracker=_KVCacheTracker(),
            schema_overrides={},
            accumulators=accumulators,
        ):
            pass
    assert survived == ["b_ran"]
    assert accumulators["extras"] == ["still here"]


# -- _stage_workflow_attachment ------------------------------------------


def test_stage_attachment_happy_path_with_data_bytes():
    att = {
        "filename": "out.mp3",
        "mime": "audio/mpeg",
        "data": b"\xff\xfb",
        "source": "workflow:tts",
        "workflow_id": "tts",
    }
    staged = _stage_workflow_attachment(att, "tts")
    assert staged is not None
    assert staged["data"] == b"\xff\xfb"
    assert staged["filename"] == "out.mp3"
    assert staged["source"] == "workflow:tts"


def test_stage_attachment_rejects_impersonation_via_source():
    att = {
        "filename": "x.bin",
        "mime": "application/octet-stream",
        "data": b"x",
        "source": "workflow:other",
        "workflow_id": "other",
    }
    assert _stage_workflow_attachment(att, "tts") is None


def test_stage_attachment_rejects_impersonation_via_workflow_id():
    att = {
        "filename": "x.bin",
        "mime": "application/octet-stream",
        "data": b"x",
        "source": "workflow:tts",
        "workflow_id": "other",
    }
    assert _stage_workflow_attachment(att, "tts") is None


def test_stage_attachment_rejects_both_data_and_path():
    att = {
        "filename": "x.bin",
        "mime": "application/octet-stream",
        "data": b"x",
        "path": "/tmp/x",
        "source": "workflow:tts",
        "workflow_id": "tts",
    }
    assert _stage_workflow_attachment(att, "tts") is None


def test_stage_attachment_rejects_neither_data_nor_path():
    att = {
        "filename": "x.bin",
        "mime": "application/octet-stream",
        "source": "workflow:tts",
        "workflow_id": "tts",
    }
    assert _stage_workflow_attachment(att, "tts") is None


def test_stage_attachment_rejects_empty_data():
    att = {
        "filename": "x.bin",
        "mime": "application/octet-stream",
        "data": b"",
        "source": "workflow:tts",
        "workflow_id": "tts",
    }
    assert _stage_workflow_attachment(att, "tts") is None


def test_stage_attachment_normalizes_path_to_bytes(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"on-disk-bytes")
    att = {
        "filename": "blob.bin",
        "mime": "application/octet-stream",
        "path": str(p),
        "source": "workflow:tts",
        "workflow_id": "tts",
    }
    staged = _stage_workflow_attachment(att, "tts")
    assert staged is not None
    assert "path" not in staged
    assert staged["data"] == b"on-disk-bytes"


def test_stage_attachment_path_read_failure_drops_entry(tmp_path):
    missing = tmp_path / "ghost.bin"
    att = {
        "filename": "ghost.bin",
        "mime": "application/octet-stream",
        "path": str(missing),
        "source": "workflow:tts",
        "workflow_id": "tts",
    }
    assert _stage_workflow_attachment(att, "tts") is None


def test_stage_attachment_whitespace_annotation_collapses_to_none():
    att = {
        "filename": "x.bin",
        "mime": "application/octet-stream",
        "data": b"x",
        "annotation": "   \n  ",
        "source": "workflow:tts",
        "workflow_id": "tts",
    }
    staged = _stage_workflow_attachment(att, "tts")
    assert staged is not None
    assert staged["annotation"] is None


def test_stage_attachment_non_dict_rejected():
    assert _stage_workflow_attachment("not a dict", "tts") is None
    assert _stage_workflow_attachment(None, "tts") is None
    assert _stage_workflow_attachment(["list"], "tts") is None


def test_stage_attachment_bad_filename_or_mime_rejected():
    for bad in (
        {"filename": 123, "mime": "x", "data": b"x", "source": "workflow:tts", "workflow_id": "tts"},
        {"filename": "x", "mime": None, "data": b"x", "source": "workflow:tts", "workflow_id": "tts"},
    ):
        assert _stage_workflow_attachment(bad, "tts") is None


def test_stage_attachment_dict_consumption_metadata_passes_through():
    att = {
        "filename": "x.bin",
        "mime": "application/octet-stream",
        "data": b"x",
        "source": "workflow:tts",
        "workflow_id": "tts",
        "consumption_metadata": {"cues": [0.5, 1.0]},
    }
    staged = _stage_workflow_attachment(att, "tts")
    assert staged is not None
    assert staged["consumption_metadata"] == {"cues": [0.5, 1.0]}


def test_stage_attachment_null_consumption_metadata_passes_through():
    att = {
        "filename": "x.bin",
        "mime": "application/octet-stream",
        "data": b"x",
        "source": "workflow:tts",
        "workflow_id": "tts",
        "consumption_metadata": None,
    }
    staged = _stage_workflow_attachment(att, "tts")
    assert staged is not None
    assert staged["consumption_metadata"] is None


def test_stage_attachment_non_dict_consumption_metadata_coerces_to_none_without_rejecting():
    for bad_cm in ("string", 42, [1, 2, 3], True):
        att = {
            "filename": "x.bin",
            "mime": "application/octet-stream",
            "data": b"x",
            "source": "workflow:tts",
            "workflow_id": "tts",
            "consumption_metadata": bad_cm,
        }
        staged = _stage_workflow_attachment(att, "tts")
        assert staged is not None, f"non-dict consumption_metadata {bad_cm!r} should not reject the attachment"
        assert staged["consumption_metadata"] is None


# -- _run_pipeline post-pipeline iteration --------------------------------


async def test_run_pipeline_emits_single_result_with_staged_attachments():
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        for ch in ["draft "]:
            yield {"type": "content", "delta": ch}
        yield {"type": "content", "delta": "body."}

    async def post_hook(post_ctx):
        yield {
            "type": "attach_artifact",
            "attachment": {
                "filename": "tts.mp3",
                "mime": "audio/mpeg",
                "data": b"mp3-bytes",
                "source": "workflow:tts",
                "workflow_id": "tts",
            },
        }
        yield {
            "type": "attach_artifact",
            "attachment": {
                "filename": "transcript.txt",
                "mime": "text/plain",
                "data": b"transcript",
                "source": "workflow:tts",
                "workflow_id": "tts",
            },
        }

    w = make_workflow(
        "tts",
        post_pipeline=post_hook,
        produces_artifacts=True,
        regenerate=lambda ctx, body: [],
        reroll_gen=lambda ctx, params, seed: b"",
    )
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            events = await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    **_pipeline_kwargs(),
                )
            )

    results = [e for e in events if e["event"] == "_result"]
    assert len(results) == 1
    payload = results[0]["data"]
    assert payload["resp_text"] == "draft body."
    assert [a["filename"] for a in payload["staged_attachments"]] == ["tts.mp3", "transcript.txt"]
    assert all(a["source"] == "workflow:tts" for a in payload["staged_attachments"])


async def test_run_pipeline_drops_attach_artifact_with_mismatched_source():
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "draft"}

    async def post_hook(post_ctx):
        yield {
            "type": "attach_artifact",
            "attachment": {
                "filename": "cheat.bin",
                "mime": "application/octet-stream",
                "data": b"x",
                "source": "workflow:other",
                "workflow_id": "other",
            },
        }

    w = make_workflow(
        "tts",
        post_pipeline=post_hook,
        produces_artifacts=True,
        regenerate=lambda ctx, body: [],
        reroll_gen=lambda ctx, params, seed: b"",
    )
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            events = await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    **_pipeline_kwargs(),
                )
            )

    [result] = [e for e in events if e["event"] == "_result"]
    assert result["data"]["staged_attachments"] == []


async def test_run_pipeline_draft_replaced_emits_writer_rewrite_and_updates_result():
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "original"}

    async def post_hook(post_ctx):
        yield {"type": "draft_replaced", "draft": "rewritten"}
        # Second draft_replaced from the same hook is logged + ignored.
        yield {"type": "draft_replaced", "draft": "rewritten again"}

    w = make_workflow("rewriter", post_pipeline=post_hook)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            events = await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    **_pipeline_kwargs(),
                )
            )

    rewrites = [e for e in events if e["event"] == "writer_rewrite"]
    assert len(rewrites) == 1
    assert rewrites[0]["data"]["refined_text"] == "rewritten"

    [result] = [e for e in events if e["event"] == "_result"]
    assert result["data"]["resp_text"] == "rewritten"


async def test_run_pipeline_turn_scratch_ref_shared_pre_to_post():
    captured: dict = {}
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "ok"}

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

    w = make_workflow("scratch", pre_pipeline=pre_hook, post_pipeline=post_hook)
    with register_for_test(w):
        turn_scratch: dict = {}
        accumulators = {"merged_enabled_tools": {}, "extras": []}
        async for _ in _iterate_pre_pipeline_hooks(
            conversation_id="c1",
            history=[],
            last_user_message="hi",
            settings=_SETTINGS,
            prefix_base=_PREFIX,
            enabled_tools_pre_merge={},
            turn_scratch=turn_scratch,
            client=client,
            kv_tracker=_KVCacheTracker(),
            schema_overrides={},
            accumulators=accumulators,
        ):
            pass

        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hi",
                    prefix=_PREFIX,
                    enabled_tools=accumulators["merged_enabled_tools"],
                    turn_scratch=turn_scratch,
                    kv_tracker=_KVCacheTracker(),
                    schema_overrides={},
                )
            )

    assert captured["pre_id"] == captured["post_id"]
    assert captured["post_value"] == "stash"


async def test_run_pipeline_turn_scratch_fresh_across_turns():
    captured: list[int] = []
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "ok"}

    async def post_hook(post_ctx):
        captured.append(id(post_ctx.turn_scratch))
        return
        yield  # pragma: no cover -- generator shape

    w = make_workflow("scratch_lifetime", post_pipeline=post_hook)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            for _ in range(2):
                await _drain(
                    _run_pipeline(
                        client,
                        _SETTINGS,
                        _DIRECTOR_STATE,
                        [],
                        [],
                        "hi",
                        prefix=_PREFIX,
                        enabled_tools={},
                        turn_scratch={},
                        kv_tracker=_KVCacheTracker(),
                        schema_overrides={},
                    )
                )
    assert captured[0] != captured[1]


async def test_run_pipeline_empty_registry_emits_single_result_no_staged():
    """No workflow registered: pipeline still emits exactly one _result with
    an empty staged_attachments list. This is the load-bearing parity property
    of the post-pipeline iteration."""
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "plain draft"}

    with patch("backend.orchestrator.writer_pass", new=mock_writer):
        events = await _drain(
            _run_pipeline(
                client,
                _SETTINGS,
                _DIRECTOR_STATE,
                [],
                [],
                "hello",
                **_pipeline_kwargs(),
            )
        )

    results = [e for e in events if e["event"] == "_result"]
    assert len(results) == 1
    assert results[0]["data"]["resp_text"] == "plain draft"
    assert results[0]["data"]["staged_attachments"] == []
    # No writer-done _result, no _refined_result fired.
    assert not any(e["event"] == "_refined_result" for e in events)


async def test_run_pipeline_post_hook_exception_logged_and_pipeline_completes():
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "draft"}

    async def crasher(post_ctx):
        raise RuntimeError("post boom")
        yield  # pragma: no cover -- generator shape

    w = make_workflow("crasher", post_pipeline=crasher)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            events = await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hi",
                    **_pipeline_kwargs(),
                )
            )

    [result] = [e for e in events if e["event"] == "_result"]
    assert result["data"]["resp_text"] == "draft"


async def test_run_pipeline_writer_abort_emits_result_skips_post_pipeline():
    """A writer-pass abort still produces persistence via a final _result,
    but the post-pipeline iteration is skipped entirely so a downstream
    hook never sees an aborted turn."""
    client = _make_client()
    post_ran = []

    async def mock_writer(c, *args, **kwargs):
        c.abort()
        yield {"type": "content", "delta": "partial"}

    async def post_hook(post_ctx):
        post_ran.append(True)
        return
        yield  # pragma: no cover -- generator shape

    w = make_workflow("never_runs", post_pipeline=post_hook)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            events = await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hi",
                    **_pipeline_kwargs(),
                )
            )

    [result] = [e for e in events if e["event"] == "_result"]
    assert result["data"]["resp_text"] == "partial"
    assert result["data"]["staged_attachments"] == []
    assert post_ran == []


async def test_run_pipeline_set_message_state_collected_and_not_forwarded():
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "draft"}

    async def post_hook(post_ctx):
        yield {"type": "set_message_state", "state": {"seen": 1}}

    w = make_workflow("ms", post_pipeline=post_hook)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            events = await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    **_pipeline_kwargs(),
                )
            )

    [result] = [e for e in events if e["event"] == "_result"]
    assert result["data"]["staged_message_state"] == {"ms": {"seen": 1}}
    assert not any(e.get("type") == "set_message_state" for e in events)
    assert not any(e.get("event") == "set_message_state" for e in events)


async def test_run_pipeline_set_message_state_non_dict_dropped():
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "draft"}

    async def post_hook(post_ctx):
        yield {"type": "set_message_state", "state": "not-a-dict"}

    w = make_workflow("ms", post_pipeline=post_hook)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            events = await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    **_pipeline_kwargs(),
                )
            )

    [result] = [e for e in events if e["event"] == "_result"]
    assert result["data"]["staged_message_state"] == {}


async def test_run_pipeline_set_message_state_keyed_per_workflow():
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "draft"}

    async def hook_a(post_ctx):
        yield {"type": "set_message_state", "state": {"from": "a"}}

    async def hook_b(post_ctx):
        yield {"type": "set_message_state", "state": {"from": "b"}}

    wa = make_workflow("wf_a", post_pipeline=hook_a)
    wb = make_workflow("wf_b", post_pipeline=hook_b)
    with register_for_test(wa), register_for_test(wb):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            events = await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    **_pipeline_kwargs(),
                )
            )

    [result] = [e for e in events if e["event"] == "_result"]
    assert result["data"]["staged_message_state"] == {"wf_a": {"from": "a"}, "wf_b": {"from": "b"}}


async def test_post_pipeline_ctx_carries_readonly_history():
    captured = {}
    client = _make_client()

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "draft"}

    async def post_hook(post_ctx):
        captured["history"] = post_ctx.history
        yield {"event": "noop", "data": {}}

    w = make_workflow("hist", post_pipeline=post_hook)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            await _drain(
                _run_pipeline(
                    client,
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    history=[{"role": "user", "content": "earlier"}],
                    **_pipeline_kwargs(),
                )
            )

    history = captured["history"]
    assert [m["role"] for m in history] == ["user"]
    assert history[0]["content"] == "earlier"
    with pytest.raises(AttributeError):
        history.append({"role": "user"})
    with pytest.raises(TypeError):
        history[0]["content"] = "x"


async def test_post_pipeline_set_message_state_persists_to_assistant_row(client):
    await create_conversation("cms", "T", "X", "")
    user_id, _ = await add_message("cms", "user", "hi", 0)
    await set_active_leaf("cms", user_id)

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "reply"}

    async def post_hook(post_ctx):
        yield {"type": "set_message_state", "state": {"k": 1}}

    w = make_workflow("ms_persist", post_pipeline=post_hook)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            pipeline = _run_pipeline(
                _make_client(),
                _SETTINGS,
                _DIRECTOR_STATE,
                [],
                [],
                "hi",
                conversation_id="cms",
                **_pipeline_kwargs(),
            )
            await _drain(_consume_pipeline(pipeline, "cms", _SETTINGS, user_id, 1))

    msgs = await get_messages("cms")
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    assert await get_workflow_message_state(assistant["id"], "ms_persist") == {"k": 1}


async def test_post_pipeline_set_message_state_dropped_when_no_message_persisted(client):
    await create_conversation("cms_empty", "T", "X", "")
    user_id, _ = await add_message("cms_empty", "user", "hi", 0)
    await set_active_leaf("cms_empty", user_id)

    async def mock_writer(c, *args, **kwargs):
        return
        yield  # pragma: no cover -- async generator with no output

    async def post_hook(post_ctx):
        yield {"type": "set_message_state", "state": {"k": 1}}

    w = make_workflow("ms_empty", post_pipeline=post_hook)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            pipeline = _run_pipeline(
                _make_client(),
                _SETTINGS,
                _DIRECTOR_STATE,
                [],
                [],
                "hi",
                conversation_id="cms_empty",
                **_pipeline_kwargs(),
            )
            await _drain(_consume_pipeline(pipeline, "cms_empty", _SETTINGS, user_id, 1))

    msgs = await get_messages("cms_empty")
    assert [m for m in msgs if m["role"] == "assistant"] == []
