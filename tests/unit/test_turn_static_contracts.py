"""Check host payloads, narrowing and the internal/public persistence seam."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

from backend.pipeline.events import PROTECTED_TURN_EVENTS, SHARED_HOOK_EVENTS

ROOT = Path(__file__).resolve().parents[2]

_SOURCE = """\
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, assert_type
from fastapi import Request
from backend.api.deps import pipeline_sse_response, sse_stream
from backend.inference import AbortToken, KVCacheTracker, LLMClient
from backend.pipeline import (
    handle_turn, handle_speak, handle_fork_edit, handle_regenerate,
    handle_super_regenerate, handle_magic_rewrite,
)
from backend.pipeline.events import (
    CoreTurnEvent, HookEvent, PipelineEvent, PublicTurnEvent,
    ResultEvent, SpeakerDoneData, TurnStateEvent,
)
from backend.pipeline.failures import reported_once, staged
from backend.pipeline.orchestrator import run_pipeline
from backend.pipeline.persistence import consume_pipeline, conversation_log_writer
from backend.pipeline.state import TurnResultData, TurnState

async def boundaries(
    client: LLMClient, events: AsyncIterator[PipelineEvent],
    public: AsyncIterator[PublicTurnEvent], req: Request,
    internal: AsyncIterator[ResultEvent | TurnStateEvent],
) -> None:
    assert_type(run_pipeline(client, {}, {"conversation_id": "c", "keywords": [], "macro_choices": {}, "active_moods": []}, [], [], "hi", prefix=[],
                            enabled_tools={}, turn_scratch={}, kv_tracker=KVCacheTracker(),
                            schema_overrides={}), AsyncIterator[PipelineEvent])
    assert_type(consume_pipeline(events, "c", {}, 1, 2), AsyncIterator[PublicTurnEvent])
    assert_type(reported_once(public, None), AsyncIterator[PublicTurnEvent])
    assert_type(staged("writer", events), AsyncIterator[PipelineEvent])
    assert_type(handle_turn("c", "hi"), AsyncIterator[PublicTurnEvent])
    assert_type(handle_speak("c", "m"), AsyncIterator[PublicTurnEvent])
    assert_type(handle_fork_edit("c", 1, "hi"), AsyncIterator[PublicTurnEvent])
    assert_type(handle_regenerate("c", 2), AsyncIterator[PublicTurnEvent])
    assert_type(handle_super_regenerate("c", 2), AsyncIterator[PublicTurnEvent])
    assert_type(handle_magic_rewrite("c", 2, "steer"), AsyncIterator[PublicTurnEvent])
    assert_type(sse_stream(public, req), AsyncGenerator[str, None])
    pipeline_sse_response(lambda tok: handle_turn("c", "hi", abort_token=tok), req, "c")
    sse_stream(events, req)  # rejected
    sse_stream(internal, req)  # rejected
    pipeline_sse_response(lambda tok: events, req, "c")  # rejected
    assert_type(TurnState().as_result_event_data(), TurnResultData)
    res = TurnState(**TurnState().as_result_event_data())
    assert_type(res.resp_text, str)
    await conversation_log_writer("c", 1)(res, None)
    await conversation_log_writer("c", 1)(res, "wrong id")  # rejected
    async for event in events:
        if isinstance(event, HookEvent):
            assert_type(event["data"], Any)
        elif event["event"] == "_turn_state":
            assert_type(event["data"], TurnState)
            event["data"]["resp_text"]  # rejected
        elif event["event"] == "_result":
            assert_type(event["data"], TurnResultData)
            assert_type(event["data"]["resp_text"], str)
            event["data"]["resp_text"] = 1  # rejected
            event["data"]["res_text"]  # rejected
        elif event["event"] == "token":
            assert_type(event["data"], str)
            event["data"]["draft"]  # rejected
        elif event["event"] == "speaker_done":
            assert_type(event["data"], SpeakerDoneData)
            assert_type(event["data"]["message_id"], int | None)
        elif event["event"] == "done":
            event["data"]  # rejected

async def declarations() -> AsyncIterator[PublicTurnEvent]:
    yield {"event": "token", "data": "text"}
    yield {"event": "writer_rewrite", "data": {"refined_text": "final"}}
    yield {"event": "draft_update", "data": {"draft": "preview"}}
    yield {"event": "reasoning", "data": {"pass": "writer", "delta": "thought"}}
    yield {"event": "phase_status", "data": {"channel": "workflow:x", "state": "done"}}
    yield {"event": "user_message_created", "data": {"id": 1, "content": "hi"}}
    yield {"event": "writer_done", "data": {"editor_will_run": False}}
    yield {"event": "feedback", "data": {"values": {"feature": [1, 2]}}}
    yield {"event": "speaking_plan", "data": {"exchange_id": "x", "plan": []}}
    yield {"event": "warning", "data": {"headline": "failed", "sentence": "why", "kind": "provider", "stage": "writer"}}
    yield {"event": "error", "data": "legacy"}
    yield {"event": "done"}
    yield HookEvent({"event": "custom", "data": {"feature": [1, 2]}})
    yield {"event": "custom", "data": {"feature": [1, 2]}}  # rejected
    yield {"event": "token", "data": 1}  # rejected
    yield {"event": "writer_rewrite", "data": {"refined_text": 1}}  # rejected
    yield {"event": "writer_rewrite", "data": {"refined_txt": "typo"}}  # rejected
    yield {"event": "reasoning", "data": {"pass": "writer", "delta": 1}}  # rejected
    yield {"event": "user_message_created", "data": {"id": "bad", "content": "hi"}}  # rejected
    yield {"event": "writer_done", "data": {"editor_will_run": "yes"}}  # rejected
    yield {"event": "speaking_plan", "data": {"exchange_id": "x", "plan": [{"member_id": 1}]}}  # rejected
    yield {"event": "done", "data": {"fake": True}}  # rejected
    yield {"event": "_turn_state", "data": TurnState()}  # rejected
    yield {"event": "_result", "data": TurnState().as_result_event_data()}  # rejected
"""


def test_turn_contracts_check_payloads_and_public_handoff(tmp_path):
    source = tmp_path / "turn_contracts.py"
    source.write_text(_SOURCE, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--project", str(ROOT / "pyrightconfig.json"), "--outputjson", str(source)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode in (0, 1), result.stdout + result.stderr
    errors = [d for d in json.loads(result.stdout)["generalDiagnostics"] if d["severity"] == "error"]
    expected = {i for i, line in enumerate(_SOURCE.splitlines()) if line.endswith("# rejected")}
    actual = {d["range"]["start"]["line"] for d in errors if Path(d["file"]) == source}
    assert actual == expected, json.dumps(errors, indent=2)
    assert all(Path(d["file"]) == source for d in errors), json.dumps(errors, indent=2)


def test_ownership_covers_turn_emitters_and_browser_dispatcher():
    """A newly dispatched core event must receive an ownership decision."""
    emitted = set()
    for path in (ROOT / "backend/pipeline").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "event" and isinstance(value, ast.Constant):
                        emitted.add(value.value)
    dispatched = set(re.findall(r'(?:case |event === )"(\w+)"', (ROOT / "frontend/chat_stream.js").read_text()))
    public = {name for name in emitted | dispatched if not name.startswith("_")}
    assert public - SHARED_HOOK_EVENTS == PROTECTED_TURN_EVENTS
