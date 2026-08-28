"""Spawning a llama-server child, on whatever event loop we were given.

ADAPTED FROM ProseRewriterWebUI's threads-and-http.client original. Orb is
async, so the thread-per-request pool and the ``queue.Queue`` fan-in are gone:
``asyncio.create_subprocess_exec`` and one drain task — except on Windows,
where asyncio cannot spawn a process at all under the loop Orb runs on.
:func:`_can_spawn_async` explains why and :class:`_ThreadChild` is the
fallback, which brings the reference's log-drain thread back with it.

Log lines are *pushed* to a sink rather than pulled, because the two
implementations disagree about which thread they arrive on and pushing is the
only shape that does not make that the caller's problem.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from . import binary


def _can_spawn_async() -> bool:
    """Whether the running event loop implements ``create_subprocess_exec``.

    Only Windows ever says no, and it says it in the worst way available: a
    bare ``NotImplementedError`` off ``BaseEventLoop._make_subprocess_transport``,
    no message, several frames inside asyncio. Windows implements subprocesses
    on the **Proactor** loop alone, and uvicorn selects the **Selector** loop on
    win32 whenever ``--reload`` or ``--workers`` is in play
    (``uvicorn/loops/asyncio.py``) -- which is exactly what ``run_windows.bat``
    passes. So this is not an exotic configuration to survive: it is every
    Windows install that uses the shipped launcher, and :class:`_ThreadChild` is
    the ordinary path there rather than a degraded one.

    Probed rather than assumed from ``os.name``, so a Windows user who runs Orb
    without ``--reload`` still gets the cheaper path. ``binary.IS_WINDOWS`` is
    read as a module attribute rather than imported by value: it is the seam
    the only test of this branch patches, and binding it at import would make
    that test pass on the constant instead of on the branch.
    """
    if not binary.IS_WINDOWS:
        return True
    proactor = getattr(asyncio, "ProactorEventLoop", None)  # Windows-only symbol
    return proactor is not None and isinstance(asyncio.get_running_loop(), proactor)


def _decode(raw: bytes) -> str:
    """One log line as text.

    Decoded as UTF-8 explicitly: llama.cpp writes UTF-8, and on Windows the
    locale code page cannot represent most of what a GGUF's metadata puts in
    that log -- a decode error here would kill the only reader that could have
    told us why the child refused to load.
    """
    return raw.decode("utf-8", "replace").rstrip()


class Child(Protocol):
    """A spawned llama-server, reduced to what the client asks of it."""

    async def start(self, argv: Sequence[str]) -> None: ...

    @property
    def returncode(self) -> int | None:
        """Exit status, or ``None`` while it runs. Never blocks -- ``wait_ready``
        asks on every poll of a boot that can take five minutes."""
        ...

    async def wait(self, timeout: float) -> bool:
        """Wait for exit. ``False`` when *timeout* elapsed first."""
        ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def aclose(self) -> None:
        """Stop reading the log. Called once the process is already gone."""
        ...


class _AsyncChild:
    """``asyncio.create_subprocess_exec`` plus a drain task. The good path."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink
        self._process: asyncio.subprocess.Process | None = None
        self._drain: asyncio.Task | None = None

    async def start(self, argv: Sequence[str]) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._drain = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        async for raw in process.stdout:
            self._sink(_decode(raw))

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    async def wait(self, timeout: float) -> bool:
        if self._process is None:
            return True
        try:
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    def terminate(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()

    def kill(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.kill()

    async def aclose(self) -> None:
        drain, self._drain = self._drain, None
        if drain is not None:
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain


class _ThreadChild:
    """``subprocess.Popen`` plus a reader thread, for a loop that cannot spawn.

    Nothing blocking runs on the event loop: the thread's entire job is reading
    the pipe, and both waits go through :func:`asyncio.to_thread`. The thread is
    a daemon because a child that ignores ``kill`` must not hold up interpreter
    exit -- ``shutdown()`` has already done all it can by then.
    """

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None

    async def start(self, argv: Sequence[str]) -> None:
        self._process = subprocess.Popen(  # noqa: S603 — local executable; request choices use closed argv allowlists
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # The child is a console program and Orb may have been started from
            # a shortcut rather than a console: without this a black window sits
            # on the desktop for as long as the model is loaded. Zero everywhere
            # else, where Popen rejects a non-zero value outright.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(target=self._pump, name="orb-llama-log", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            # `iter(readline, b"")` rather than iterating the file: a pipe read
            # ahead in block-sized chunks would hold back the very lines a boot
            # failure is diagnosed from until the buffer filled.
            for raw in iter(process.stdout.readline, b""):
                self._sink(_decode(raw))
        finally:
            with contextlib.suppress(Exception):
                process.stdout.close()

    @property
    def returncode(self) -> int | None:
        # poll(), not `.returncode`: Popen only fills that attribute in when
        # something asks, so reading it bare reports a dead child as running.
        return None if self._process is None else self._process.poll()

    async def wait(self, timeout: float) -> bool:
        process = self._process
        if process is None:
            return True

        def _wait() -> bool:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
            return True

        return await asyncio.to_thread(_wait)

    def terminate(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

    def kill(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()

    async def aclose(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            # The read loop ends at EOF, which is the child's death, so by the
            # time this is called the join is only collecting last words. Still
            # bounded: a child that survived kill() must not hang shutdown.
            await asyncio.to_thread(reader.join, 5.0)


async def spawn(argv: Sequence[str], sink: Callable[[str], None]) -> Child:
    """Start *argv*, whichever way this event loop is able to."""
    child: Child = _AsyncChild(sink) if _can_spawn_async() else _ThreadChild(sink)
    await child.start(argv)
    return child
