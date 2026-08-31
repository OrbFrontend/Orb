"""Own the current llama-server child for one feature."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from . import binary, manager
from .client import LaunchProfile, LlamaServerClient

logger = logging.getLogger(__name__)


class ManagedLlamaServerHost:
    """One resident llama-server, loaded lazily and swapped by profile."""

    def __init__(self, *, name: str, idle_timeout: float, register: bool = True) -> None:
        """*register* is default-on because the failure mode of forgetting it is
        the one this subsystem warns about three times: an orphaned child
        holding the GPU after Orb exits. A test that builds a throwaway host
        passes ``register=False``."""
        self.name = name
        self.state = "idle"  # idle | loading | ready | failed
        self.error = ""
        self.profile: LaunchProfile | None = None
        self.server: LlamaServerClient | None = None
        self._idle_timeout = idle_timeout
        self._lock = asyncio.Lock()
        self._inflight = 0
        self._idle = asyncio.Condition()
        self._stale = False
        self._idle_task: asyncio.Task | None = None
        self._last_used = time.monotonic()
        if register:
            manager.register(self)

    def mark_stale(self, profile: LaunchProfile | None) -> None:
        """Record a new selection without touching the running child.

        A settings route calls this and returns immediately: a turn may be
        mid-generation, and a settings write has no business blocking on it or
        killing it. The restart happens on the next :meth:`ensure`. An
        identical profile is not a change, which is what stops a settings write
        that altered nothing from restarting a healthy child.
        """
        if self.profile is not None and profile is not None and self.profile == profile:
            return
        self.profile, self._stale = profile, True

    @property
    def healthy(self) -> bool:
        return not self._stale and self.server is not None and self.server.alive and self.server.ready

    async def ensure(self, profile: LaunchProfile) -> LlamaServerClient:
        """The running client for *profile*, starting or restarting as needed."""
        async with self._lock:
            return await self._ensure_locked(profile)

    async def _ensure_locked(self, profile: LaunchProfile) -> LlamaServerClient:
        """``ensure`` with the swap lock already held."""
        if self.profile == profile and self.healthy and self.server is not None:
            return self.server
        executable = binary.find_binary()
        # The flag goes up BEFORE the drain, not after it: new work has to
        # stop arriving for the drain to end, and `state` is what callers
        # read to turn themselves away with a message.
        self.profile, self.state, self.error, self._stale = profile, "loading", "", False
        await self._drain()
        if self.server is not None:
            await self.server.stop()
            self.server = None
        logger.info(
            "Loading %s (%d MB, %d slots, gpu=%s)…",
            os.path.basename(profile.model_path),
            profile.size_mb,
            profile.parallel,
            profile.gpu_layers > 0,
        )
        server = LlamaServerClient(profile, executable)
        try:
            await server.start()
            await server.wait_ready()
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
    async def use(self, profile: LaunchProfile):
        """Yield a client protected from config-driven reloads.

        The in-flight count is raised before the swap lock is released. This
        closes the small but real gap an ensure-then-account sequence would
        leave, where a Settings change could otherwise stop a child that a
        caller had just received but had not started sending requests to yet.
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

    async def _drain(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        async with self._idle:
            while self._inflight and time.monotonic() < deadline:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._idle.wait(), timeout=0.25)

    async def release(self) -> None:
        """Release the current child and reload it on demand."""
        async with self._lock:
            if self.server is None:
                self._stale = True
                return
            await self._drain()
            await self.server.stop()
            self.server = None
            self.state = "idle"
            self._stale = True

    async def shutdown(self) -> None:
        """Stop the child and the idle watcher. Reached from the app lifespan
        through :func:`manager.shutdown_all`."""
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
                model = os.path.basename(self.server.profile.model_path)
                logger.info("%s idle for %.0fs; unloading %s", self.name, self._idle_timeout, model)
                await self.server.stop()
                self.server = None
                self.state = "idle"
                self._idle_task = None
                return
