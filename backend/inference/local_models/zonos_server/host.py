"""Own the ZONOS3 server child process: start it on demand, stop it when idle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from ..llama_server import Child, manager, spawn
from . import binary

logger = logging.getLogger(__name__)

BOOT_TIMEOUT = 300.0
_STOP_TIMEOUT = 10.0


class ZonosServerFailed(RuntimeError):
    """The server did not start. Carries the message the panel shows."""


@dataclass(frozen=True)
class Launch:
    """What one server process is started with; a different launch restarts it."""

    backbone: str
    decoder: str
    steering: str
    encoder: str = ""
    teacher: str = ""
    gpu: bool = True

    def argv(self, executable: str, port: int) -> list[str]:
        args = [executable, self.backbone, "--zac", self.decoder, "--steer", self.steering]
        if self.encoder and self.teacher:
            args += ["--zac-encoder", self.encoder, "--ssl-teacher", self.teacher]
        return [*args, "--host", "127.0.0.1", "--port", str(port), "--gpu" if self.gpu else "--cpu"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ZonosServerHost:
    """One resident zonos3-server, loaded lazily and restarted when its launch changes."""

    def __init__(self, *, name: str, idle_timeout: float) -> None:
        self.name = name
        self.state = "idle"  # idle | loading | ready | failed
        self.error = ""
        self._idle_timeout = idle_timeout
        self._launch: Launch | None = None
        self._child: Child | None = None
        self._url = ""
        self._log: deque[str] = deque(maxlen=40)
        self._lock = asyncio.Lock()
        self._inflight = 0
        self._idle = asyncio.Condition()
        self._idle_task: asyncio.Task | None = None
        self._last_used = time.monotonic()
        manager.register(self)

    @property
    def _alive(self) -> bool:
        return self._child is not None and self._child.returncode is None

    async def _stop(self) -> None:
        child, self._child, self._url = self._child, None, ""
        if child is None:
            return
        child.terminate()
        if not await child.wait(_STOP_TIMEOUT):
            child.kill()
            await child.wait(_STOP_TIMEOUT)
        await child.aclose()

    async def _wait_ready(self, child: Child, url: str) -> None:
        deadline = time.monotonic() + BOOT_TIMEOUT
        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.monotonic() < deadline:
                if child.returncode is not None:
                    tail = "\n".join(self._log) or f"exit status {child.returncode}"
                    raise ZonosServerFailed(f"The ZONOS3 server stopped while starting:\n{tail[-600:]}")
                with contextlib.suppress(httpx.HTTPError):
                    if (await client.get(f"{url}/v2/capabilities")).status_code == 200:
                        return
                await asyncio.sleep(0.25)
        raise ZonosServerFailed(f"The ZONOS3 server did not answer within {BOOT_TIMEOUT:.0f} s.")

    async def _ensure_locked(self, launch: Launch) -> str:
        if self._launch == launch and self._alive and self.state == "ready":
            return self._url
        executable = binary.find_binary()
        self._launch, self.state, self.error = launch, "loading", ""
        try:
            await self._drain()
            await self._stop()
            self._log.clear()
            port = _free_port()
            started = time.monotonic()
            self._child = await spawn(launch.argv(str(executable), port), self._log.append)
            url = f"http://127.0.0.1:{port}"
            await self._wait_ready(self._child, url)
        except asyncio.CancelledError:
            self.state = "idle"
            with contextlib.suppress(Exception):
                await self._stop()
            raise
        except Exception as exc:
            self.state, self.error = "failed", str(exc)
            with contextlib.suppress(Exception):
                await self._stop()
            raise
        self._url, self.state = url, "ready"
        self._last_used = time.monotonic()
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch())
        logger.info("%s ready in %.1fs on %s (gpu=%s)", self.name, time.monotonic() - started, url, launch.gpu)
        return url

    @contextlib.asynccontextmanager
    async def use(self, launch: Launch) -> AsyncIterator[str]:
        """Yield the server's base URL, and keep the server running for the block."""
        async with self._lock:
            url = await self._ensure_locked(launch)
            async with self._idle:
                self._inflight += 1
        try:
            yield url
        finally:
            async with self._idle:
                self._inflight -= 1
                self._last_used = time.monotonic()
                self._idle.notify_all()

    async def _drain(self, timeout: float = 120.0) -> bool:
        deadline = time.monotonic() + timeout
        async with self._idle:
            while self._inflight and time.monotonic() < deadline:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._idle.wait(), timeout=0.25)
            return not self._inflight

    @contextlib.asynccontextmanager
    async def exclusive_release(self) -> AsyncIterator[None]:
        """Stop the server and keep it stopped through a model file change; refuses while a request is running."""
        async with self._lock:
            if not await self._drain():
                raise TimeoutError("The ZONOS3 server is still in use; drain timed out")
            await self._stop()
            self.state = "idle"
            yield

    async def release(self) -> None:
        """Stop the server; the next use starts it again."""
        async with self._lock:
            await self._drain()
            await self._stop()
            self.state = "idle"

    async def shutdown(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._idle_task
            self._idle_task = None
        await self._stop()
        self.state = "idle"

    async def _idle_watch(self) -> None:
        while True:
            await asyncio.sleep(min(30.0, max(5.0, self._idle_timeout / 4)))
            if self._child is None:
                return
            if self._inflight or time.monotonic() - self._last_used < self._idle_timeout:
                continue
            async with self._lock:
                if self._inflight or self._child is None or time.monotonic() - self._last_used < self._idle_timeout:
                    continue
                logger.info("%s idle for %.0fs; stopping the ZONOS3 server", self.name, self._idle_timeout)
                await self._stop()
                self.state = "idle"
                self._idle_task = None
                return
