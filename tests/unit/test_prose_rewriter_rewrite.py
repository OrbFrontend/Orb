"""Progress ordering for the concurrent local prose rewriter."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from backend.inference.prose_rewriter import rewrite

pytestmark = pytest.mark.asyncio

FIRST = "First " + "source " * 16
SECOND = "Second " + "source " * 16
DRAFT = f"{FIRST}\n\n{SECOND}"
BOTH_REWRITTEN = "First rewrite.\n\nSecond rewrite."


def _source(prompt: str) -> str:
    return prompt.split("<|im_start|>source\n", 1)[1].split("<|im_end|>", 1)[0]


class _Server:
    def __init__(self, *, first_delay: float, second_delay: float) -> None:
        self.first_delay = first_delay
        self.second_delay = second_delay

    async def count_tokens(self, _source: str) -> int:
        return 10

    async def generate(self, prompt: str, **_kwargs) -> tuple[str, bool]:
        if _source(prompt).startswith("First"):
            await asyncio.sleep(self.first_delay)
            return "First rewrite.", True
        await asyncio.sleep(self.second_delay)
        return "Second rewrite.", True


class _FlakyServer:
    """The first paragraph dies; the second is still decoding when it does.

    A real child that falls over takes every in-flight paragraph with it, so
    this is the ordinary failure, not a corner of one.
    """

    def __init__(self) -> None:
        self.second_finished = False

    async def count_tokens(self, _source: str) -> int:
        return 10

    async def generate(self, prompt: str, **_kwargs) -> tuple[str, bool]:
        if _source(prompt).startswith("First"):
            raise RuntimeError("the child died")
        await asyncio.sleep(0.2)
        self.second_finished = True
        return "Second rewrite.", True


class _Host:
    slots = 2

    def __init__(self, *, first_delay: float = 0, second_delay: float = 0, server=None) -> None:
        self.server = server or _Server(first_delay=first_delay, second_delay=second_delay)

    async def ensure(self, _variant, _gpu: bool):
        return self.server

    @asynccontextmanager
    async def acquire(self):
        yield


async def _rewrite(**delays) -> tuple[str, list[str]]:
    """``arewrite`` over a two-paragraph draft; returns the result and every snapshot."""
    updates: list[str] = []
    rewritten = await rewrite.arewrite(
        DRAFT, variant=object(), host=_Host(**delays), on_progress=lambda snapshot: _record(updates, snapshot)
    )
    return rewritten, updates


async def _record(updates: list[str], snapshot: str) -> None:
    updates.append(snapshot)


async def test_progress_waits_for_the_first_unfinished_paragraph():
    """The second paragraph lands first, but its snapshot waits for the one above."""
    rewritten, updates = await _rewrite(first_delay=0.01)
    assert rewritten == BOTH_REWRITTEN
    assert updates == [BOTH_REWRITTEN]


async def test_progress_emits_the_top_paragraph_before_later_ones():
    rewritten, updates = await _rewrite(second_delay=0.01)
    assert rewritten == BOTH_REWRITTEN
    assert updates == [f"First rewrite.\n\n{SECOND.strip()}", BOTH_REWRITTEN]


async def test_a_failed_paragraph_takes_its_siblings_down_with_it():
    """``gather`` alone raises the first exception and lets the rest run on, past
    the point where the caller has reported the failure and released its
    in-flight slot — so the host is free to stop the child underneath them."""
    host = _Host(server=_FlakyServer())

    # Verbatim, not wrapped in an ExceptionGroup: this string is the warning
    # the user reads.
    with pytest.raises(RuntimeError, match="the child died"):
        await rewrite.arewrite(DRAFT, variant=object(), host=host)

    assert host.server.second_finished is False
    await asyncio.sleep(0.3)  # comfortably past the sibling's own sleep
    assert host.server.second_finished is False, "the sibling outlived the call that failed"
