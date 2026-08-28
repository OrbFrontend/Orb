"""Spawning the llama-server child on both kinds of event loop, and its argv.

The threaded path is Windows-only in production (see
``process._can_spawn_async``). A branch that only ever runs on the platform CI
does not cover is how ``NotImplementedError`` reached a user's chat bubble in
the first place, so both implementations are exercised here against the same
expectations.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from backend.inference.local_models.llama_server import binary as B
from backend.inference.local_models.llama_server import client as C
from backend.inference.local_models.llama_server import process as P

pytestmark = pytest.mark.asyncio

# A stand-in child: prints two lines (one non-ASCII, to pin the UTF-8 decode
# that a Windows code page would otherwise mangle) then blocks until killed.
CHATTY = "import sys,time\nprint('boot ok');print('café ✓');sys.stdout.flush()\ntime.sleep(60)\n"
QUICK = "import sys\nsys.stdout.write('bye\\n')\nsys.exit(3)\n"

IMPLEMENTATIONS = [P._AsyncChild, P._ThreadChild]


def _argv(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def _profile(**overrides) -> C.LaunchProfile:
    fields = {
        "model_id": "test",
        "model_path": "/models/test.gguf",
        "alias": "test-feature",
        "gpu_layers": 999,
        "ctx_size": 2560,
        "parallel": 2,
        "http_threads": 8,
    }
    return C.LaunchProfile(**{**fields, **overrides})


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


@pytest.mark.parametrize(("can_spawn", "expected"), [(True, P._AsyncChild), (False, P._ThreadChild)])
async def test_spawn_picks_the_implementation_the_loop_can_support(monkeypatch, can_spawn, expected):
    monkeypatch.setattr(P, "_can_spawn_async", lambda: can_spawn)
    child = await P.spawn(_argv(QUICK), lambda _line: None)
    assert isinstance(child, expected)
    await child.wait(timeout=20)
    await child.aclose()


async def test_can_spawn_async_rejects_only_a_windows_loop_that_is_not_proactor(monkeypatch):
    """The exact configuration ``run_windows.bat`` produces: win32 + --reload,
    which uvicorn answers with a SelectorEventLoop that cannot spawn.

    Patched on ``binary``, the module that defines the flag, because
    ``process`` reads it as an attribute rather than binding it at import —
    which is what keeps this test on the branch instead of on the constant.
    """
    monkeypatch.setattr(P.binary, "IS_WINDOWS", False)
    assert P._can_spawn_async() is True

    class _NotProactor:  # stands in for asyncio.ProactorEventLoop on a POSIX box
        pass

    monkeypatch.setattr(P.binary, "IS_WINDOWS", True)
    monkeypatch.setattr(P.asyncio, "ProactorEventLoop", _NotProactor, raising=False)
    assert P._can_spawn_async() is False


async def test_boot_failure_reports_the_child_log(monkeypatch, tmp_path):
    """``stop()`` closes the drain *after* the process is reaped precisely so
    this tail is not empty — it is the whole diagnostic for a bad GGUF or a
    Vulkan build with no loader."""
    monkeypatch.setattr(B, "supports_flag", lambda _binary, _flag: False)
    monkeypatch.setattr(C, "_free_port", lambda: 12345)
    server = C.LlamaServerClient(_profile(), tmp_path / "llama-server")
    server.argv = _argv("import sys\nprint('CUDA error: no device')\nsys.exit(1)\n")
    await server.start()
    with pytest.raises(RuntimeError, match="CUDA error: no device"):
        await server.wait_ready(timeout=20)
    await server.stop()


async def test_argv_is_read_off_the_profile_and_nothing_else(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "supports_flag", lambda _binary, _flag: True)
    argv = C._argv(_profile(gpu_layers=0, ctx_size=1280, parallel=1, http_threads=6), tmp_path / "llama-server", 4242)

    assert argv[0] == str(tmp_path / "llama-server")
    assert argv[argv.index("--model") + 1] == "/models/test.gguf"
    assert argv[argv.index("--alias") + 1] == "test-feature"
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "4242"
    assert argv[argv.index("--n-gpu-layers") + 1] == "0"
    assert argv[argv.index("--ctx-size") + 1] == "1280"
    assert argv[argv.index("--parallel") + 1] == "1"
    assert argv[argv.index("--threads-http") + 1] == "6"
    assert "--cont-batching" in argv


async def test_the_web_ui_flag_is_only_sent_to_a_build_that_knows_it(monkeypatch, tmp_path):
    """A flag an older llama-server has never heard of is not a warning, it is
    an immediate exit with a usage message."""
    monkeypatch.setattr(B, "supports_flag", lambda _binary, _flag: False)
    assert "--no-webui" not in C._argv(_profile(), tmp_path / "llama-server", 1)

    monkeypatch.setattr(B, "supports_flag", lambda _binary, _flag: True)
    assert "--no-webui" in C._argv(_profile(), tmp_path / "llama-server", 1)
    assert "--no-webui" not in C._argv(_profile(no_webui=False), tmp_path / "llama-server", 1)


async def test_a_profile_number_that_did_not_come_from_code_is_refused():
    """The barrier that keeps a persisted string off a command line. ``bool`` is
    an ``int``, so the check is on the exact type."""
    for field in ("gpu_layers", "ctx_size", "parallel", "http_threads"):
        with pytest.raises(TypeError, match="code-owned ints"):
            _profile(**{field: "4"})
    with pytest.raises(TypeError, match="code-owned ints"):
        _profile(parallel=True)
