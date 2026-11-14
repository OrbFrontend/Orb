"""Own the current server child for one feature."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Generic, Protocol, TypeVar

from . import binary, manager
from .client import LaunchProfile, LlamaServerClient

logger = logging.getLogger(__name__)


class Server(Protocol):
    """One child server process, as its host drives it."""

    port: int
    started_at: float
    ready: bool

    @property
    def alive(self) -> bool: ...

    async def start(self) -> None: ...

    async def wait_ready(self) -> None: ...

    async def stop(self) -> None: ...


P = TypeVar("P")
S = TypeVar("S", bound=Server)


class ManagedServerHost(Generic[P, S]):
    """One resident server, loaded lazily and swapped by launch profile *P*; a subclass says how *P* becomes a server."""

    def __init__(self, *, name: str, idle_timeout: float) -> None:
        self.name = name
        self.state = "idle"  # idle | loading | ready | failed
        self.error = ""
        self.profile: P | None = None
        self.server: S | None = None
        self._idle_timeout = idle_timeout
        self._lock = asyncio.Lock()
        self._inflight = 0
        self._idle = asyncio.Condition()
        self._stale = False
        self._idle_task: asyncio.Task | None = None
        self._last_used = time.monotonic()
        manager.register(self)

    def mark_stale(self, profile: P | None) -> None:
        """Record a new selection without touching the running child.

        A settings route calls this and returns immediately: a turn may be mid-generation, and a settings write has no business
        blocking on it or killing it. The restart happens on the next :meth:`ensure`. An identical profile is not a change,
        which is what stops a settings write that altered nothing from restarting a healthy child.
        """
        if self.profile is not None and profile is not None and self.profile == profile:
            return
        self.profile, self._stale = profile, True

    @property
    def healthy(self) -> bool:
        return not self._stale and self.server is not None and self.server.alive and self.server.ready

    def _new_server(self, profile: P) -> S:
        """An unstarted server for *profile*; raises when it cannot be built."""
        raise NotImplementedError

    async def ensure(self, profile: P) -> S:
        """The running client for *profile*, starting or restarting as needed."""
        async with self._lock:
            return await self._ensure_locked(profile)

    async def _ensure_locked(self, profile: P) -> S:
        """``ensure`` with the swap lock already held."""
        if self.profile == profile and self.healthy and self.server is not None:
            return self.server
        server = self._new_server(profile)
        # The flag goes up BEFORE the drain, not after it: new work has to stop arriving for the drain to end, and `state` is
        # what callers read to turn themselves away with a message.
        self.profile, self.state, self.error, self._stale = profile, "loading", "", False
        try:
            await self._drain()
            if self.server is not None:
                await self.server.stop()
                self.server = None
            await server.start()
            await server.wait_ready()
        except asyncio.CancelledError:
            # An abandoned load (an aborted turn, the feature switched off) is not a failure, but nothing else holds its child:
            # without the stop it keeps its VRAM for good and the panel reads "loading" forever.
            previous = self.server
            live = previous is not None and previous.alive and previous.ready
            self.state, self._stale = ("ready" if live else "idle"), True
            with contextlib.suppress(Exception):
                await server.stop()
            raise
        except Exception as exc:
            self.state, self.error = "failed", str(exc)
            with contextlib.suppress(Exception):
                await server.stop()
            raise
        self.server = server
        self.state = "ready"
        self._last_used = time.monotonic()
        self._start_idle_watch()
        logger.info("%s ready in %.1fs on 127.0.0.1:%d", self.name, time.monotonic() - server.started_at, server.port)
        return server

    @contextlib.asynccontextmanager
    async def use(self, profile: P) -> AsyncIterator[S]:
        """Yield a client protected from config-driven reloads.

        The in-flight count is raised before the swap lock is released. This closes the small but real gap an
        ensure-then-account sequence would leave, where a Settings change could otherwise stop a child that a caller had just
        received but had not started sending requests to yet.
        """
        async with self._lock:
            server = await self._ensure_locked(profile)
            async with self._idle:
                self._inflight += 1
        try:
            yield server
        finally:
            async with self._idle:
                self._inflight -= 1
                self._last_used = time.monotonic()
                self._idle.notify_all()

    async def _drain(self, timeout: float = 120.0) -> bool:
        """Wait, bounded, for in-flight users; whether every one finished."""
        deadline = time.monotonic() + timeout
        async with self._idle:
            while self._inflight and time.monotonic() < deadline:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._idle.wait(), timeout=0.25)
            return not self._inflight

    async def _release_locked(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None
            self.state = "idle"
        self._stale = True

    @contextlib.asynccontextmanager
    async def exclusive_release(self) -> AsyncIterator[None]:
        """Drain users and keep admission closed through a model file mutation.

        Unlike a swap or a plain release, a file mutation refuses rather than stopping a child someone is still using.
        """
        async with self._lock:
            if not await self._drain():
                raise TimeoutError("Local model is still in use; drain timed out")
            await self._release_locked()
            yield

    async def release(self) -> None:
        """Release the current child and reload it on demand."""
        async with self._lock:
            await self._drain()
            await self._release_locked()

    async def shutdown(self) -> None:
        """Stop the child and the idle watcher. Reached from the app lifespan through :func:`manager.shutdown_all`."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._idle_task
            self._idle_task = None
        if self.server is not None:
            await self.server.stop()
            self.server = None
        self.state = "idle"

    def _start_idle_watch(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch())

    async def _idle_watch(self) -> None:
        """Stop the child after the idle timeout at zero in-flight, freeing VRAM."""
        while True:
            await asyncio.sleep(min(30.0, max(5.0, self._idle_timeout / 4)))
            if self.server is None:
                return
            if self._inflight or time.monotonic() - self._last_used < self._idle_timeout:
                continue
            async with self._lock:
                if self._inflight or self.server is None:
                    continue
                if time.monotonic() - self._last_used < self._idle_timeout:
                    continue
                logger.info("%s idle for %.0fs; stopping its server", self.name, self._idle_timeout)
                await self.server.stop()
                self.server = None
                self.state = "idle"
                self._idle_task = None
                return


class ManagedLlamaServerHost(ManagedServerHost[LaunchProfile, LlamaServerClient]):
    """One resident llama-server."""

    def _new_server(self, profile: LaunchProfile) -> LlamaServerClient:
        # THE GPU SWITCH IS THIS LINE, not `--n-gpu-layers` alone. Every build accepts that flag and a CPU-only one then
        # offloads nothing, silently and with a zero exit status, so asking for the build that can honour it is what makes the
        # setting real.
        wants_gpu = profile.gpu_layers > 0
        executable = binary.find_binary(gpu=wants_gpu)
        if wants_gpu and binary.gpu_capable(executable) is False:
            logger.warning("%s was asked for GPU but %s reports no GPU device; it will run on CPU.", self.name, executable)
        logger.info(
            "Loading %s (%d MB, %d slots, gpu=%s)…",
            os.path.basename(profile.model_path),
            profile.size_mb,
            profile.parallel,
            wants_gpu,
        )
        return LlamaServerClient(profile, executable)
