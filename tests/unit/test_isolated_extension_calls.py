"""Bounds specific to prefix-free community-extension model calls."""

from __future__ import annotations

import pytest

from backend.inference.isolated import IsolatedCallError, isolated_structured


class _StructuredClient:
    def __init__(self, value):
        self.value = value

    async def complete(self, **_kwargs):
        yield {
            "type": "done",
            "message": {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "extension_output",
                            "arguments": self.value,
                        },
                    }
                ]
            },
        }


async def test_structured_tool_arguments_count_toward_the_output_byte_cap():
    with pytest.raises(IsolatedCallError, match="exceeded 10 bytes"):
        await isolated_structured(
            _StructuredClient({"text": "x" * 1000}),
            model="test",
            prompt="return text",
            schema={"type": "object"},
            max_tokens=100,
            max_output_bytes=10,
        )


async def test_structured_output_below_the_cap_is_returned_decoded():
    value = await isolated_structured(
        _StructuredClient({"n": 3}),
        model="test",
        prompt="return n",
        schema={"type": "object"},
        max_tokens=100,
        max_output_bytes=100,
    )
    assert value == {"n": 3}
