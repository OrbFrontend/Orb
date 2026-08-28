"""The current child, and the only thing allowed to replace it.

THE LOCK GUARDS THE SWAP, NOT THE GENERATION. Callers take a reference to the
running client through :meth:`ManagedLlamaServerHost.use` and then talk to it
without holding anything, which is what makes concurrent requests concurrent. A
swap waits for the in-flight count to reach zero before it kills anything, so
work in progress is never cut off by someone changing a selector in Settings.

MODEL SWITCHING IS A RESTART. llama.cpp cannot swap weights inside a running
server, so a different :class:`LaunchProfile` — another checkpoint, a GPU/CPU
flip, a different lane count — stops the child and starts a new one. The host
sets ``state = "loading"`` BEFORE draining in-flight work: new work has to stop
arriving for the drain to end.

ONE HOST IS ONE RESIDENT MODEL, so a host belongs to the feature that owns it
rather than to this package. Two features sharing one would each drain and
restart the other's model. What IS shared is the registry in :mod:`manager`,
which exists only for the operations that touch every child at once: app
shutdown, and replacing the binary underneath them.

THE HOST HAS NO OPINION ABOUT WHAT IS IN THE PROFILE. Lane counts, context
sizes and model paths are the feature's business, validated by the feature's
own allowlist before the profile is constructed.
"""

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

    # ── loading ──────────────────────────────────────────────────────────────

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
        """Let go of the files the child holds, and reload lazily on next use.

        FOR THE FILE OPERATIONS THAT CANNOT RUN AROUND A LIVE CHILD. llama.cpp
        mmaps the GGUF and Windows refuses to unlink a mapped file or a running
        executable, so deleting a model or replacing the binary fails there
        with a bare sharing violation and no exit but restarting Orb; elsewhere
        the unlink succeeds and leaves the child serving weights that are gone.

        Drains first, so work in flight finishes rather than being cut off
        — the same courtesy ``ensure`` pays a profile swap; the lock holds new
        work off meanwhile, which is why ``state`` still says ``ready`` until
        the child is actually gone. Marks stale so the next ``ensure`` reloads
        even though the selection has not changed.
        """
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

    # ── idle unload ──────────────────────────────────────────────────────────

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
