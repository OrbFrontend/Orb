"""Regression tests for stop-generation abort propagation through the pipeline.

Verifies that aborting during the director pass prevents the writer pass from firing, and aborting during the writer pass
prevents the editor pass from firing.

Also verifies the error-abort corner case: a genuine error in any of the three passes aborts the pipeline (the exception
propagates out of ``run_pipeline``) rather than being swallowed, so a failed pass ends the turn just like a manual abort does --
never producing a half-processed draft.
"""

import asyncio
import contextlib
import sqlite3
from unittest.mock import patch

import pytest

from backend.inference import AbortToken, KVCacheTracker, LLMClient
from backend.pipeline import entrypoints, persistence
from backend.pipeline.failures import STAGE_SAVE, mark_stage, reported_once, stage_of
from backend.pipeline.orchestrator import run_pipeline
from backend.pipeline.passes.director import DirectorResult
from backend.pipeline.state import TurnState

_DIRECTOR_STATE = {"active_moods": []}
_PREFIX = [{"role": "system", "content": "You are an assistant."}]


def _make_client() -> LLMClient:
    return LLMClient("http://localhost:9999")


def _pipeline_kwargs(enabled_tools: dict) -> dict:
    """Bundle the keyword-only kwargs the orchestrator wrapper supplies to
    ``run_pipeline``: final pipeline prefix, merged enable map, per-turn
    workflow scratch dict, and the KV tracker."""
    return {
        "prefix": _PREFIX,
        "enabled_tools": dict(enabled_tools),
        "turn_scratch": {},
        "kv_tracker": KVCacheTracker(),
        "schema_overrides": {},
    }


async def _drain(gen) -> list[dict]:
    return [e async for e in gen]


def _settings(tool: str) -> dict:
    return {"model_name": "test", "enable_agent": 1, "enabled_tools": {tool: True}, "reasoning_enabled_passes": {}}


async def _run(client, tool: str, *, director=None, writer=None, editor=None, reported: bool = False) -> list[dict]:
    """Drive ``run_pipeline`` with the named passes patched.

    ``editor_apply_patch`` is not a Director-loop tool, so the Director is skipped; a non-None phrase_bank makes do_edit=True.
    """
    settings = _settings(tool)
    extra = {"phrase_bank": [[]]} if tool == "editor_apply_patch" else {}
    gen = run_pipeline(
        client, settings, _DIRECTOR_STATE, [], [], "hello", **extra, **_pipeline_kwargs(settings["enabled_tools"])
    )
    passes = {
        "backend.pipeline.passes.director.director.director_pass": director,
        "backend.pipeline.passes.writer.writer_pass": writer,
        "backend.pipeline.passes.editor.editor.editor_pass": editor,
    }
    with contextlib.ExitStack() as stack:
        for target, fake in passes.items():
            if fake is not None:
                stack.enter_context(patch(target, new=fake))
        return await _drain(reported_once(gen, client.abort_token) if reported else gen)


def _counting(calls: list, event: dict):
    async def fake_pass(*args, **kwargs):
        calls.append(1)
        yield event

    return fake_pass


def _raising(message: str, before=None):
    async def fake_pass(*args, **kwargs):
        if before:
            before()
        raise RuntimeError(message)
        yield  # pragma: no cover -- makes this an async generator

    return fake_pass


def _drafting(text: str, *, abort: bool = False):
    async def fake_pass(c, *args, **kwargs):
        if abort:
            c.abort()
        yield {"type": "content", "delta": text}

    return fake_pass


class TestAbortPropagation:
    async def test_abort_after_director_skips_writer_pass(self):
        writer_calls: list = []

        async def mock_director(c, *args, **kwargs):
            c.abort()
            yield {"type": "done", "result": DirectorResult()}

        writer = _counting(writer_calls, {"type": "content", "delta": "should not appear"})
        await _run(_make_client(), "direct_scene", director=mock_director, writer=writer)
        assert writer_calls == [], "writer pass must not fire after director-phase abort"

    async def test_abort_after_writer_skips_editor_pass(self):
        editor_calls: list = []
        editor = _counting(editor_calls, {"type": "done", "draft": "edited"})
        await _run(_make_client(), "editor_apply_patch", writer=_drafting("partial text", abort=True), editor=editor)
        assert editor_calls == [], "editor pass must not fire after writer-phase abort"


class TestErrorAborts:
    """A genuine error in the Director or Writer aborts the turn (exception
    escapes run_pipeline) instead of being swallowed and pressed on. The
    Editor only refines a finished draft, so its failure is a warning."""

    async def test_director_error_aborts_and_skips_writer(self):
        writer_calls: list = []
        writer = _counting(writer_calls, {"type": "content", "delta": "should not appear"})
        with pytest.raises(RuntimeError, match="director endpoint exploded"):
            await _run(_make_client(), "direct_scene", director=_raising("director endpoint exploded"), writer=writer)
        assert writer_calls == [], "writer must not fire after a director-pass error"

    async def test_editor_error_warns_and_completes_the_turn(self):
        """The turn keeps the Writer's draft and still completes with ``_result``."""
        events = await _run(
            _make_client(),
            "editor_apply_patch",
            writer=_drafting("the full draft"),
            editor=_raising("editor endpoint exploded"),
        )
        warning = next(e["data"] for e in events if e["event"] == "warning")
        assert warning["headline"] == "The Editor didn't finish."
        assert warning["stage"] == "editor pass"
        assert "editor endpoint exploded" in warning["sentence"]
        assert next(e["data"] for e in events if e["event"] == "_result")["resp_text"] == "the full draft"

    async def test_an_editor_error_after_stop_is_not_reported(self):
        """A failure racing the user's Stop is explained by the Stop itself; the stopped turn still saves the draft."""
        client = _make_client()
        events = await _run(
            client,
            "editor_apply_patch",
            writer=_drafting("the full draft"),
            editor=_raising("stream closed by stop", before=client.abort),
            reported=True,
        )
        assert not [e for e in events if e["event"] == "warning"]
        assert next(e["data"] for e in events if e["event"] == "_result")["resp_text"] == "the full draft"


class TestStoppedTurnPersistence:
    """What a stopped turn commits: its reply exactly once, nothing when it has
    no reply, and a failed save reported even though the turn was stopped."""

    async def test_a_cancel_during_the_save_waits_for_it_and_never_saves_twice(self, monkeypatch):
        """Cancelled after the INSERT committed but before the save returned: the
        same save finishes, no fallback inserts the reply again, and the log row
        is written once for the row that exists."""
        inserts: list[str] = []
        fallbacks: list[tuple] = []
        logs: list[int | None] = []
        committed, release = asyncio.Event(), asyncio.Event()

        async def slow_persist(conversation_id, res, *args, **kwargs):
            inserts.append(res.resp_text)
            committed.set()
            await release.wait()  # still staging proposals, counters, message state
            return 7, [], []

        async def fallback(*args, **kwargs):
            fallbacks.append(args)
            return True

        async def log(res, asst_id):
            logs.append(asst_id)

        monkeypatch.setattr(persistence, "_persist_result", slow_persist)
        monkeypatch.setattr(persistence, "_fallback_persist", fallback)

        async def pipeline():
            yield {"event": "_result", "data": TurnState(resp_text="the reply").as_result_event_data()}

        async def consume():
            async for _ in persistence.consume_pipeline(pipeline(), "c1", {}, 1, 2, extra_on_result=log):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(committed.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert inserts == ["the reply"]
        assert fallbacks == []
        assert logs == [7]

    async def test_a_stopped_turn_whose_fallback_save_fails_reports_the_save_error(self, monkeypatch):
        """The call the stop cut short fails on the way out, and the fallback
        save of the prose already streamed fails too. The stop explains the
        first; nothing explains the lost reply, so that is what surfaces."""

        async def failing_persist(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(persistence, "_persist_result", failing_persist)

        async def pipeline():
            yield {"event": "_turn_state", "data": TurnState(resp_text="streamed prose")}
            raise RuntimeError("stream closed by stop")

        with pytest.raises(sqlite3.OperationalError) as caught:
            async for _ in persistence.consume_pipeline(pipeline(), "c1", {}, 1, 2):
                pass

        assert stage_of(caught.value) == STAGE_SAVE
        assert isinstance(caught.value.__cause__, RuntimeError)

    async def test_an_empty_attempt_leaves_the_directors_committed_moods_alone(self, monkeypatch):
        """A regeneration starts from the branch's earlier moods; stopped before
        any prose, it must not write those over the committed ones."""
        writes: list[object] = []

        async def update_director_state(*args, **kwargs):
            writes.append(args)

        monkeypatch.setattr(persistence.db, "update_director_state", update_director_state)
        assert (await persistence._persist_result("c1", TurnState(resp_text="  "), {"enable_agent": 1}, 1, 2)) == (None, [], [])
        assert writes == []

    async def test_a_failure_after_stop_ends_quietly_but_a_failed_save_is_reported(self, monkeypatch):
        async def context(*args, **kwargs):
            return object()

        monkeypatch.setattr(entrypoints, "load_pipeline_context", context)
        token = AbortToken()
        token.abort()

        async def cut_short(_ctx):
            raise RuntimeError("stream closed by stop")
            yield  # pragma: no cover -- makes this an async generator

        async def save_failed(_ctx):
            error = RuntimeError("database is locked")
            mark_stage(error, STAGE_SAVE)
            raise error
            yield  # pragma: no cover -- makes this an async generator

        stopped = [e async for e in entrypoints._run_turn_handler("c1", token, cut_short, log_label="Test")]
        failed = [e async for e in entrypoints._run_turn_handler("c1", token, save_failed, log_label="Test")]

        assert stopped == [{"event": "done"}]
        assert [e["event"] for e in failed] == ["error"]
        assert failed[0]["data"]["stage"] == STAGE_SAVE
