"""Spawning the llama-server child on both kinds of event loop.

The threaded path is Windows-only in production (see ``server._can_spawn_async``).
A branch that only ever runs on the platform CI does not cover is how
``NotImplementedError`` reached a user's chat bubble in the first place, so both
implementations are exercised here against the same expectations.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import pytest

from backend.inference.prose_rewriter import server as S

pytestmark = pytest.mark.asyncio

# A stand-in child: prints two lines (one non-ASCII, to pin the UTF-8 decode
# that a Windows code page would otherwise mangle) then blocks until killed.
CHATTY = "import sys,time\nprint('boot ok');print('café ✓');sys.stdout.flush()\ntime.sleep(60)\n"
QUICK = "import sys\nsys.stdout.write('bye\\n')\nsys.exit(3)\n"

IMPLEMENTATIONS = [S._AsyncChild, S._ThreadChild]


def _argv(script: str) -> list[str]:
    return [sys.executable, "-c", script]


async def _lines(sink: list[str], want: int, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while len(sink) < want and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("impl", IMPLEMENTATIONS)
async def test_child_streams_log_lines_and_stops(impl):
    sink: list[str] = []
    child = impl(sink.append)
    await child.start(_argv(CHATTY))
    try:
        await _lines(sink, 2)
        assert sink[:2] == ["boot ok", "café ✓"]
        assert child.returncode is None  # still running
    finally:
        await child.wait(0)  # no-op; proves a zero timeout does not hang
        child.terminate()
        assert await child.wait(timeout=15) is True
        await child.aclose()
    assert child.returncode is not None


@pytest.mark.parametrize("impl", IMPLEMENTATIONS)
async def test_returncode_reports_an_exit_without_being_waited_on(impl):
    """``wait_ready`` polls ``returncode`` to notice a child that died loading,
    and ``Popen`` only fills that attribute in when something calls ``poll()``."""
    sink: list[str] = []
    child = impl(sink.append)
    await child.start(_argv(QUICK))
    deadline = asyncio.get_running_loop().time() + 20.0
    while child.returncode is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert child.returncode == 3
    await child.aclose()
    assert sink == ["bye"]


@pytest.mark.parametrize("impl", IMPLEMENTATIONS)
async def test_stop_is_idempotent_and_terminate_survives_a_dead_child(impl):
    child = impl(lambda _line: None)
    await child.start(_argv(QUICK))
    assert await child.wait(timeout=20) is True
    child.terminate()  # already gone; must not raise
    child.kill()
    assert await child.wait(timeout=5) is True
    await child.aclose()
    await child.aclose()


@pytest.mark.parametrize(("can_spawn", "expected"), [(True, S._AsyncChild), (False, S._ThreadChild)])
async def test_spawn_picks_the_implementation_the_loop_can_support(monkeypatch, can_spawn, expected):
    monkeypatch.setattr(S, "_can_spawn_async", lambda: can_spawn)
    child = await S.spawn(_argv(QUICK), lambda _line: None)
    assert isinstance(child, expected)
    await child.wait(timeout=20)
    await child.aclose()


async def test_can_spawn_async_rejects_only_a_windows_loop_that_is_not_proactor(monkeypatch):
    """The exact configuration ``run_windows.bat`` produces: win32 + --reload,
    which uvicorn answers with a SelectorEventLoop that cannot spawn."""
    monkeypatch.setattr(S.runtime, "IS_WINDOWS", False)
    assert S._can_spawn_async() is True

    class _NotProactor:  # stands in for asyncio.ProactorEventLoop on a POSIX box
        pass

    monkeypatch.setattr(S.runtime, "IS_WINDOWS", True)
    monkeypatch.setattr(S.asyncio, "ProactorEventLoop", _NotProactor, raising=False)
    assert S._can_spawn_async() is False


async def test_llama_server_boot_failure_reports_the_child_log(monkeypatch, tmp_path):
    """``stop()`` closes the drain *after* the process is reaped precisely so
    this tail is not empty — it is the whole diagnostic for a bad GGUF or a
    Vulkan build with no loader."""
    monkeypatch.setattr(S, "_help_text", lambda _binary: "")
    monkeypatch.setattr(S, "_free_port", lambda: 12345)
    variant = S.catalog.variants()[0]
    server = S.LlamaServer(variant, tmp_path / "llama-server", slots=1, gpu=False)
    server.argv = _argv("import sys\nprint('CUDA error: no device')\nsys.exit(1)\n")
    await server.start()
    with pytest.raises(RuntimeError, match="CUDA error: no device"):
        await server.wait_ready(timeout=20)
    await server.stop()


@pytest.mark.parametrize("slots", [1, 2, 3, 4])
async def test_parallel_slots_select_only_fixed_command_arguments(monkeypatch, tmp_path, slots):
    """One configured paragraph lane maps to one full CTX_PER_SLOT KV lane."""
    monkeypatch.setattr(S, "_help_text", lambda _binary: "")
    monkeypatch.setattr(S, "_free_port", lambda: 12345)
    variant = S.catalog.variants()[0]
    server = S.LlamaServer(variant, tmp_path / "llama-server", slots=slots, gpu=True)

    alias = server.argv.index("--alias")
    parallel = server.argv.index("--parallel")
    context = server.argv.index("--ctx-size")
    threads = server.argv.index("--threads-http")
    assert server.argv[alias + 1] == "prose-rewriter"
    assert server.argv[parallel + 1] == str(slots)
    assert server.argv[context + 1] == str(slots * S.CTX_PER_SLOT)
    assert server.argv[threads + 1] == str(slots * 2 + 4)


async def test_llama_server_rejects_a_variant_outside_the_registry(tmp_path):
    """A request-selected id must resolve to a code-owned registry row before
    any part of it is allowed into the child command."""
    variant = replace(S.catalog.variants()[0], id="--host", path="/tmp/attacker.gguf")

    with pytest.raises(ValueError, match="Unregistered prose-rewriter variant"):
        S.LlamaServer(variant, tmp_path / "llama-server", slots=2, gpu=False)


async def test_batch_size_change_marks_the_loaded_host_stale():
    host = S.ModelHost(slots=4)
    variant = S.catalog.variants()[0]
    host.variant = variant
    host.gpu = True
    host._stale = False

    host.mark_stale(variant, True, 2)

    assert host.slots == 2
    assert host.healthy is False


async def test_host_rejects_batch_sizes_outside_the_supported_range():
    with pytest.raises(ValueError, match="slots must be between 1 and 4"):
        S.ModelHost(slots=0)
    with pytest.raises(ValueError, match="slots must be between 1 and 4"):
        S.ModelHost(slots=5)


class _StoppableServer:
    """Stands in for a loaded LlamaServer; records that it was stopped."""

    def __init__(self) -> None:
        self.alive = True
        self.ready = True
        self.stopped = False

    async def stop(self) -> None:
        self.alive = False
        self.ready = False
        self.stopped = True


async def test_release_waits_for_an_in_flight_rewrite_before_stopping_the_child():
    """Deleting a GGUF or replacing the binary has to let go of the files first
    — Windows will not unlink a mapped weight or a running executable — but it
    must not cut off a rewrite already decoding, the way a bare stop would."""
    host = S.ModelHost()
    host.server = _StoppableServer()
    host.state = "ready"
    variant = S.catalog.variants()[0]
    host.variant = variant
    order: list[str] = []

    async def rewriting() -> None:
        async with host.use(variant, True, 4):
            await asyncio.sleep(0.05)
            order.append("rewrite finished")

    async def releasing() -> None:
        await asyncio.sleep(0)  # let the rewrite take its slot first
        assert host._inflight == 1
        await host.release()
        order.append("released")

    server = host.server
    await asyncio.gather(rewriting(), releasing())

    assert order == ["rewrite finished", "released"]
    assert server.stopped is True
    assert host.server is None
    assert host.state == "idle"


async def test_release_with_no_child_still_forces_the_next_load():
    """The file may have been deleted while nothing was loaded; the next
    ``ensure`` must not trust a 'healthy' it inherited from before."""
    host = S.ModelHost()
    await host.release()
    assert host.healthy is False
