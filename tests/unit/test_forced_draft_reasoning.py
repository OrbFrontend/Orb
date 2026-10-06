"""Require forced_draft callers to choose reasoning explicitly.

Missing hints default thinking on and can consume the answer budget. Check
both the required signature and the transport interpretation of emitted params.
"""

import inspect
from typing import Any

import pytest

from backend.inference.drafting import forced_draft
from backend.inference.text_completion import reasoning_enabled

TOOL: dict[str, Any] = {
    "type": "function",
    "function": {"name": "do_thing", "parameters": {"type": "object", "properties": {}}},
}


class _CapturingClient:
    def __init__(self):
        self.params: dict = {}

    async def complete(self, **kwargs):
        self.params = kwargs
        yield {
            "type": "done",
            "message": {"tool_calls": [{"id": "c", "type": "function", "function": {"name": "do_thing", "arguments": "{}"}}]},
        }


def test_reasoning_on_has_no_default():
    """No 'leave it alone' value: a new call site cannot inherit the bug by simply not mentioning reasoning."""
    param = inspect.signature(forced_draft).parameters["reasoning_on"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


async def test_omitting_it_is_a_type_error():
    with pytest.raises(TypeError):
        await forced_draft(  # type: ignore[call-arg]
            _CapturingClient(), "m", system="s", user="u", tool=TOOL, max_tokens=64
        )


@pytest.mark.parametrize("on", [True, False])
async def test_the_transport_reads_back_what_was_asked_for(on: bool):
    """The params the helper emits are the ones the text transport interprets --
    asserted through ``reasoning_enabled`` rather than by restating the dict, so
    a change to either side fails here instead of silently disagreeing."""
    client = _CapturingClient()
    await forced_draft(client, "m", system="s", user="u", tool=TOOL, max_tokens=64, reasoning_on=on)  # type: ignore[arg-type]
    assert reasoning_enabled(client.params) is on


def test_an_unpinned_call_would_have_meant_thinking_on():
    """Why the parameter is required, stated as the transport states it."""
    assert reasoning_enabled({"temperature": 0.2, "max_tokens": 512}) is True
