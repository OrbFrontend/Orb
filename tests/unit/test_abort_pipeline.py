"""
Regression tests for stop-generation abort propagation through the pipeline.

Verifies that aborting during the director pass prevents the writer pass from
firing, and aborting during the writer pass prevents the editor pass from firing.
"""

from __future__ import annotations

from unittest.mock import patch

from backend.llm_client import LLMClient
from backend.orchestrator import _run_pipeline


_DIRECTOR_STATE = {"active_moods": []}
_PREFIX = [{"role": "system", "content": "You are an assistant."}]


def _make_client() -> LLMClient:
    return LLMClient("http://localhost:9999")


async def _drain(gen) -> list[dict]:
    return [e async for e in gen]


class TestAbortPropagation:

    async def test_abort_after_director_skips_writer_pass(self):
        """Writer pass must not be called when abort is signalled during the director pass."""
        client = _make_client()
        writer_calls = [0]

        async def mock_director(c, *args, **kwargs):
            c.abort()
            yield {"type": "done", "result": ([], "", [], 0, None, {})}

        async def mock_writer(*args, **kwargs):
            writer_calls[0] += 1
            yield {"type": "content", "delta": "should not appear"}

        settings = {
            "model_name": "test",
            "enable_agent": 1,
            "enabled_tools": {"direct_scene": True},
            "reasoning_enabled_passes": {},
        }

        with patch("backend.orchestrator._director_pass", new=mock_director), patch(
            "backend.orchestrator._writer_pass", new=mock_writer
        ):
            await _drain(
                _run_pipeline(
                    client, settings, _DIRECTOR_STATE, [], [], _PREFIX, "hello"
                )
            )

        assert (
            writer_calls[0] == 0
        ), "writer pass must not fire after director-phase abort"

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

        with patch("backend.orchestrator._writer_pass", new=mock_writer), patch(
            "backend.orchestrator.editor_pass", new=mock_editor
        ):
            await _drain(
                _run_pipeline(
                    client,
                    settings,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    _PREFIX,
                    "hello",
                    phrase_bank=[[]],
                )
            )

        assert (
            editor_calls[0] == 0
        ), "editor pass must not fire after writer-phase abort"
