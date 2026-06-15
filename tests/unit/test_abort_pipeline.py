"""
Regression tests for stop-generation abort propagation through the pipeline.

Verifies that aborting during the director pass prevents the writer pass from
firing, and aborting during the writer pass prevents the editor pass from firing.

Also verifies the error-abort corner case: a genuine error in any of the three
passes aborts the pipeline (the exception propagates out of ``_run_pipeline``)
rather than being swallowed, so a failed pass ends the turn just like a manual
abort does — never producing a half-processed draft.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.inference import _KVCacheTracker
from backend.inference import LLMClient
from backend.orchestrator import _run_pipeline
from backend.passes.director import DirectorResult

_DIRECTOR_STATE = {"active_moods": []}
_PREFIX = [{"role": "system", "content": "You are an assistant."}]


def _make_client() -> LLMClient:
    return LLMClient("http://localhost:9999")


def _pipeline_kwargs(enabled_tools: dict) -> dict:
    """Bundle the keyword-only kwargs the orchestrator wrapper supplies to
    ``_run_pipeline``: final pipeline prefix, merged enable map, per-turn
    workflow scratch dict, and the KV tracker."""
    return {
        "prefix": _PREFIX,
        "enabled_tools": dict(enabled_tools),
        "turn_scratch": {},
        "kv_tracker": _KVCacheTracker(),
        "schema_overrides": {},
    }


async def _drain(gen) -> list[dict]:
    return [e async for e in gen]


class TestAbortPropagation:
    async def test_abort_after_director_skips_writer_pass(self):
        """Writer pass must not be called when abort is signalled during the director pass."""
        client = _make_client()
        writer_calls = [0]

        async def mock_director(c, *args, **kwargs):
            c.abort()
            yield {"type": "done", "result": DirectorResult()}

        async def mock_writer(*args, **kwargs):
            writer_calls[0] += 1
            yield {"type": "content", "delta": "should not appear"}

        settings = {
            "model_name": "test",
            "enable_agent": 1,
            "enabled_tools": {"direct_scene": True},
            "reasoning_enabled_passes": {},
        }

        with (
            patch("backend.passes.director.director.director_pass", new=mock_director),
            patch("backend.passes.writer.writer_pass", new=mock_writer),
        ):
            await _drain(
                _run_pipeline(
                    client,
                    settings,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    **_pipeline_kwargs(settings["enabled_tools"]),
                )
            )

        assert writer_calls[0] == 0, "writer pass must not fire after director-phase abort"

    async def test_abort_after_writer_skips_editor_pass(self):
        """Editor pass must not be called when abort is signalled during the writer pass."""
        client = _make_client()
        editor_calls = [0]

        async def mock_writer(c, *args, **kwargs):
            c.abort()
            yield {"type": "content", "delta": "partial text"}

        async def mock_editor(*args, **kwargs):
            editor_calls[0] += 1
            yield {"type": "done", "draft": "edited"}

        # editor_apply_patch is a POST_WRITER_TOOL, so has_pre_writer_tools=False
        # (director pass skipped). phrase_bank not None makes do_edit=True.
        settings = {
            "model_name": "test",
            "enable_agent": 1,
            "enabled_tools": {"editor_apply_patch": True},
            "reasoning_enabled_passes": {},
        }

        with (
            patch("backend.passes.writer.writer_pass", new=mock_writer),
            patch("backend.passes.editor.editor.editor_pass", new=mock_editor),
        ):
            await _drain(
                _run_pipeline(
                    client,
                    settings,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    phrase_bank=[[]],
                    **_pipeline_kwargs(settings["enabled_tools"]),
                )
            )

        assert editor_calls[0] == 0, "editor pass must not fire after writer-phase abort"


class TestErrorAborts:
    """A genuine error in any pass aborts the turn (exception escapes
    _run_pipeline) instead of being swallowed and pressed on."""

    async def test_director_error_aborts_and_skips_writer(self):
        """An error in the director pass propagates and the writer never runs."""
        client = _make_client()
        writer_calls = [0]

        async def mock_director(*args, **kwargs):
            raise RuntimeError("director endpoint exploded")
            yield  # pragma: no cover — makes this an async generator

        async def mock_writer(*args, **kwargs):
            writer_calls[0] += 1
            yield {"type": "content", "delta": "should not appear"}

        settings = {
            "model_name": "test",
            "enable_agent": 1,
            "enabled_tools": {"direct_scene": True},
            "reasoning_enabled_passes": {},
        }

        with (
            patch("backend.passes.director.director.director_pass", new=mock_director),
            patch("backend.passes.writer.writer_pass", new=mock_writer),
        ):
            with pytest.raises(RuntimeError, match="director endpoint exploded"):
                await _drain(
                    _run_pipeline(
                        client,
                        settings,
                        _DIRECTOR_STATE,
                        [],
                        [],
                        "hello",
                        **_pipeline_kwargs(settings["enabled_tools"]),
                    )
                )

        assert writer_calls[0] == 0, "writer must not fire after a director-pass error"

    async def test_editor_error_aborts_pipeline(self):
        """An error in the editor pass propagates out instead of keeping the
        original draft and completing the turn."""
        client = _make_client()

        async def mock_writer(*args, **kwargs):
            yield {"type": "content", "delta": "the full draft"}

        async def mock_editor(*args, **kwargs):
            raise RuntimeError("editor endpoint exploded")
            yield  # pragma: no cover — makes this an async generator

        # editor_apply_patch is a POST_WRITER_TOOL → director skipped; phrase_bank
        # not None makes do_edit=True so the editor runs over the writer draft.
        settings = {
            "model_name": "test",
            "enable_agent": 1,
            "enabled_tools": {"editor_apply_patch": True},
            "reasoning_enabled_passes": {},
        }

        with (
            patch("backend.passes.writer.writer_pass", new=mock_writer),
            patch("backend.passes.editor.editor.editor_pass", new=mock_editor),
        ):
            with pytest.raises(RuntimeError, match="editor endpoint exploded"):
                await _drain(
                    _run_pipeline(
                        client,
                        settings,
                        _DIRECTOR_STATE,
                        [],
                        [],
                        "hello",
                        phrase_bank=[[]],
                        **_pipeline_kwargs(settings["enabled_tools"]),
                    )
                )
