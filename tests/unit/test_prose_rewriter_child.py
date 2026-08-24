"""Spawning the llama-server child on both kinds of event loop.

The threaded path is Windows-only in production — Windows implements asyncio
subprocesses on the Proactor loop alone, and uvicorn hands Orb a Selector loop
whenever ``--reload`` is set, which is what both shipped launchers pass. A
branch that only ever runs on the platform CI does not cover is how
``NotImplementedError`` reached a user's chat bubble in the first place, so
both implementations are exercised here against the same expectations.
"""

from __future__ import annotations

import asyncio
import sys

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
    """``wait_ready`` polls ``returncode`` to notice a child that died loading.

    ``Popen`` only fills its ``returncode`` attribute in when something calls
    ``poll()``, so the threaded implementation has to poll rather than read.
    """
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


async def test_spawn_picks_the_threaded_child_when_the_loop_cannot_spawn(monkeypatch):
    monkeypatch.setattr(S, "_can_spawn_async", lambda: False)
    child = await S.spawn(_argv(QUICK), lambda _line: None)
    assert isinstance(child, S._ThreadChild)
    await child.wait(timeout=20)
    await child.aclose()


async def test_spawn_prefers_asyncio_when_the_loop_supports_it(monkeypatch):
    monkeypatch.setattr(S, "_can_spawn_async", lambda: True)
    child = await S.spawn(_argv(QUICK), lambda _line: None)
    assert isinstance(child, S._AsyncChild)
    await child.wait(timeout=20)
    await child.aclose()


async def test_can_spawn_async_is_true_off_windows(monkeypatch):
    monkeypatch.setattr(S.runtime, "IS_WINDOWS", False)
    assert S._can_spawn_async() is True


async def test_can_spawn_async_rejects_a_windows_loop_that_is_not_proactor(monkeypatch):
    """The exact configuration ``run_windows.bat`` produces: win32 + --reload,
    which uvicorn answers with a SelectorEventLoop that cannot spawn."""
    monkeypatch.setattr(S.runtime, "IS_WINDOWS", True)

    class _NotProactor:  # stands in for asyncio.ProactorEventLoop on a POSIX box
        pass

    monkeypatch.setattr(S.asyncio, "ProactorEventLoop", _NotProactor, raising=False)
    assert S._can_spawn_async() is False


async def test_llama_server_boot_failure_reports_the_child_log(monkeypatch, tmp_path):
    """A child that exits while loading must surface its own last words.

    ``stop()`` closes the drain *after* the process is reaped precisely so this
    tail is not empty — it is the whole diagnostic for a bad GGUF or a Vulkan
    build with no loader.
    """
    monkeypatch.setattr(S, "_help_text", lambda _binary: "")
    variant = S.catalog.variants()[0]
    server = S.LlamaServer(variant, tmp_path / "llama-server", slots=1, gpu=False)
    server.argv = _argv("import sys\nprint('CUDA error: no device')\nsys.exit(1)\n")
    await server.start()
    with pytest.raises(RuntimeError, match="CUDA error: no device"):
        await server.wait_ready(timeout=20)
    await server.stop()
