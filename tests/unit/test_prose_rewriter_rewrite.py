"""Progress ordering for the concurrent local prose rewriter."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from backend.inference.prose_rewriter import rewrite

pytestmark = pytest.mark.asyncio


class _Server:
    def __init__(self, *, first_delay: float, second_delay: float) -> None:
        self.first_delay = first_delay
        self.second_delay = second_delay

    async def count_tokens(self, _source: str) -> int:
        return 10

    async def generate(self, prompt: str, **_kwargs) -> tuple[str, bool]:
        source = prompt.split("<|im_start|>source\n", 1)[1].split("<|im_end|>", 1)[0]
        if source.startswith("First"):
            await asyncio.sleep(self.first_delay)
            return "First rewrite.", True
        await asyncio.sleep(self.second_delay)
        return "Second rewrite.", True


class _Host:
    slots = 2

    def __init__(self, *, first_delay: float = 0, second_delay: float = 0) -> None:
        self.server = _Server(first_delay=first_delay, second_delay=second_delay)

    async def ensure(self, _variant, _gpu: bool) -> _Server:
        return self.server

    @asynccontextmanager
    async def acquire(self):
        yield


async def test_progress_waits_for_the_first_unfinished_paragraph():
    first = "First " + "source " * 16
    second = "Second " + "source " * 16
    draft = f"{first}\n\n{second}"
    updates: list[str] = []

    async def record(snapshot: str) -> None:
        updates.append(snapshot)

    rewritten = await rewrite.arewrite(
        draft,
        variant=object(),
        host=_Host(first_delay=0.01),
        on_progress=record,
    )

    expected = "First rewrite.\n\nSecond rewrite."
    assert rewritten == expected
    assert updates == [expected]


async def test_progress_emits_the_top_paragraph_before_later_ones():
    first = "First " + "source " * 16
    second = "Second " + "source " * 16
    draft = f"{first}\n\n{second}"
    updates: list[str] = []

    async def record(snapshot: str) -> None:
        updates.append(snapshot)

    rewritten = await rewrite.arewrite(
        draft,
        variant=object(),
        host=_Host(second_delay=0.01),
        on_progress=record,
    )

    assert rewritten == "First rewrite.\n\nSecond rewrite."
    assert updates == [f"First rewrite.\n\n{second.strip()}", rewritten]
