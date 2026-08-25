"""llama-server, supervised: one child process, one model, N parallel slots.

WHY A CHILD PROCESS RATHER THAN A BINDING. The rewriter generates one request
per paragraph and wants them decoded *together*. Reaching continuous batching
through ``llama-cpp-python`` (the binding the classifiers already use) means
driving ``llama_decode`` and the KV cache by hand from Python; reaching it
through ``llama-server`` means ``--parallel 4 --cont-batching`` and an HTTP
call. There is no third option that is less work than the second — which is
also why this feature's ``runtime`` is ``llama_server`` and it needs none of
``requirements-ml.txt`` except ``huggingface_hub`` for the download.

ADAPTED FROM ProseRewriterWebUI's threads-and-http.client original. Orb is
async, so the thread-per-paragraph pool and the ``queue.Queue`` fan-in are
gone: ``asyncio.create_subprocess_exec``, an ``httpx`` client Orb already
depends on, and one drain task — except on Windows, where asyncio cannot spawn
a process at all under the loop Orb runs on. :func:`_can_spawn_async` explains
why and :class:`_ThreadChild` is the fallback, which brings the reference's
log-drain thread back with it.

THE CHILD IS BOUND TO LOOPBACK ON AN EPHEMERAL PORT and is not the thing anyone
connects to. Its own web UI is off, and nothing here exposes the port, because
it has no authentication and ``/slots`` will hand out other people's prompts.

MODEL SWITCHING IS A RESTART. llama.cpp cannot swap weights inside a running
server, so a variant change or a GPU/CPU flip stops the child and starts a new
one. ``ModelHost`` is the only thing allowed to do that, and it sets
``state = "loading"`` BEFORE draining in-flight work — new work has to stop
arriving for the drain to end.

THIS IS ORB'S FIRST MANAGED SUBPROCESS. ``shutdown()`` is registered in the
FastAPI ``lifespan`` (``api/__init__.py``); without it an orphaned child holds
the GPU after Orb exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import httpx

from . import catalog, runtime
from .catalog import Variant

logger = logging.getLogger(__name__)

# Per slot, and the number is the trained envelope plus room to finish a
# sentence: 512 source tokens is the documented maximum input, the generation
# budget never exceeds 512, and the prompt's own three blocks are a dozen more.
# n_ctx is divided by the slot count inside llama.cpp, so this multiplies.
CTX_PER_SLOT = 1280
# Four lanes rather than eight, because that multiplication is not free: the KV
# cache is allocated in full when the model loads, and a 1280-token lane is
# 140 MB on the 1.7B and 190 MB on the 4B. Eight of them reserve well over a
# gigabyte of VRAM before the first request arrives, which is the difference
# between fitting and not on an 8 GB card.
DEFAULT_SLOTS = 4
BOOT_TIMEOUT = 300.0
STOP_TOKEN = "<|im_end|>"

#: Seconds at zero in-flight before the child is stopped and its VRAM released.
#: Matters most when the Writer is also local on the same card.
IDLE_TIMEOUT = float(os.environ.get("ORB_PROSE_REWRITER_IDLE", "300"))

_HELP_CACHE: dict[str, str] = {}


def _help_text(binary: Path) -> str:
    """``--help`` once per binary, cached, so optional flags can be probed.

    People bring their own llama-server — a distro package, a release tarball,
    a build from last year — and a flag the binary has never heard of is not a
    warning, it is an immediate exit with a usage message.
    """
    key = str(binary)
    if key not in _HELP_CACHE:
        try:
            done = subprocess.run(  # noqa: S603 — binary resolved by runtime.find_binary
                [key, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
            )
            _HELP_CACHE[key] = (done.stdout or "") + (done.stderr or "")
        except Exception:  # a binary that will not even print help fails properly at boot
            _HELP_CACHE[key] = ""
    return _HELP_CACHE[key]


def _free_port() -> int:
    """A port the child can have. The race between closing this socket and the
    child binding it is the standard one: nothing else on a single-user box is
    competing for an ephemeral port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _error_text(blob: str) -> str:
    try:
        payload = json.loads(blob)
    except ValueError:
        return blob.strip() or "llama-server reported an error"
    if isinstance(payload, dict):
        inner = payload.get("error", payload)
        return str(inner.get("message") or inner) if isinstance(inner, dict) else str(inner)
    return str(payload)


# ── spawning a child, on whatever loop we were given ─────────────────────────


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
    without ``--reload`` still gets the cheaper path.
    """
    if not runtime.IS_WINDOWS:
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
    """A spawned llama-server, reduced to what :class:`LlamaServer` asks of it.

    Log lines are *pushed* to a sink rather than pulled, because the two
    implementations disagree about which thread they arrive on and pushing is
    the only shape that does not make that the caller's problem.
    """

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
        self._process = subprocess.Popen(  # noqa: S603 — binary resolved by runtime.find_binary
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


class LlamaServer:
    """A running child, and the three endpoints this feature asks it for:
    ``/health`` while it loads, ``/tokenize`` to size a job, ``/completion``
    to run one."""

    def __init__(self, variant: Variant, binary: Path, *, slots: int, gpu: bool) -> None:
        self.variant, self.binary, self.slots = variant, binary, slots
        self.port = _free_port()
        self.started_at = time.monotonic()
        self.log: deque[str] = deque(maxlen=60)
        # Guards `log` alone. Under _ThreadChild the reader appends from its own
        # thread while `tail()` snapshots from the loop, and iterating a deque
        # mid-append raises -- in the boot-failure path, which is the one place
        # the log has to survive.
        self._log_lock = threading.Lock()
        self.ready = False
        self.child: Child | None = None
        self._client: httpx.AsyncClient | None = None
        self.argv = [
            str(binary),
            "--model",
            catalog.variant_path(variant),
            "--alias",
            variant.id,
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            # GPU vs CPU is this flag ALONE. Vulkan is a property of which
            # binary was fetched, not a runtime switch.
            "--n-gpu-layers",
            "999" if gpu else "0",
            "--ctx-size",
            str(slots * CTX_PER_SLOT),
            "--parallel",
            str(slots),
            "--cont-batching",
            "--threads-http",
            str(slots * 2 + 4),
        ]
        # Optional, and asked for only if this build has it: this feature never
        # calls /v1/chat/completions, and llama.cpp's own front end has no
        # business being reachable on a port we opened.
        if "--no-webui" in _help_text(binary):
            self.argv.append("--no-webui")

    async def start(self) -> None:
        self.child = await spawn(self.argv, self._log_line)
        self._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", timeout=30.0)

    def _log_line(self, line: str) -> None:
        """Keep the last 60 log lines so a boot failure can report *why*.

        Reached from the drain task or from the reader thread depending on how
        the child was spawned, so it may not assume it owns the loop.
        """
        with self._log_lock:
            self.log.append(line)
        logger.debug("llama | %s", line)

    def tail(self, n: int = 12) -> str:
        with self._log_lock:
            lines = list(self.log)
        return "\n".join(lines[-n:])

    @property
    def alive(self) -> bool:
        return self.child is not None and self.child.returncode is None

    async def wait_ready(self, timeout: float = BOOT_TIMEOUT) -> None:
        """Poll ``/health`` until ok, or say why it never will.

        A 4.7 GB model off a cold page cache is tens of seconds, so the timeout
        is generous; what it is really for is the case where the child died,
        which shows up here as a returncode that is no longer None.
        """
        assert self.child is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = self.child.returncode
            if code is not None:
                # Drain before reporting: the reason the child gave up is in its
                # last few lines, and the reader can still be behind them. This
                # is the one message anybody diagnoses a bad GGUF or a Vulkan
                # build with no loader from, so it does not get to be racy.
                await self.stop()
                raise RuntimeError(
                    f"llama-server exited with status {code} while loading {self.variant.local_name}:\n{self.tail()}"
                )
            try:
                response = await self._get("/health", timeout=5.0)
                if response.get("status") == "ok":
                    self.ready = True
                    return
            except Exception:  # not up yet is the common case, not an error
                pass
            await asyncio.sleep(0.25)
        await self.stop()
        raise RuntimeError(f"llama-server did not become ready within {timeout:.0f}s:\n{self.tail()}")

    async def stop(self) -> None:
        self.ready = False
        child, self.child = self.child, None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if child is None:
            return
        if child.returncode is None:
            child.terminate()
            if not await child.wait(timeout=15):
                child.kill()
                await child.wait(timeout=5)
        # AFTER the process is gone, not before. The drain is what captures a
        # dying child's last words, and `tail()` is how `wait_ready` explains a
        # boot failure — closing it first threw away the explanation.
        with contextlib.suppress(Exception):
            await child.aclose()

    # ── requests ─────────────────────────────────────────────────────────────

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("llama-server is not running")
        return self._client

    async def _get(self, path: str, timeout: float = 5.0) -> dict:
        response = await self._http().get(path, timeout=timeout)
        response.raise_for_status()
        return response.json() if response.content else {}

    async def count_tokens(self, text: str) -> int:
        """The real count from the model's own vocabulary.

        A character estimate would be free and wrong in the one direction that
        matters: the 512-token ceiling is where the model leaves the length it
        was trained on, and a paragraph waved through on an estimate degrades
        quietly instead of being passed through intact.
        """
        response = await self._http().post("/tokenize", json={"content": text}, timeout=30.0)
        if response.status_code != 200:
            raise RuntimeError(_error_text(response.text))
        return len(response.json().get("tokens") or [])

    async def generate(self, prompt: str, *, n_predict: int, temperature: float, top_p: float) -> tuple[str, bool]:
        """Stream one completion; return ``(text, stopped)``.

        ``stopped`` is whether the model ended the paragraph itself; ``text.finish``
        trims the half-sentence tail of one that merely ran out of budget.
        Cancelling the awaiting task closes the connection mid-stream, which
        llama.cpp treats as a cancellation, so Stop frees the slot at once.
        """
        payload = {
            "prompt": prompt,
            "n_predict": n_predict,
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,
            "cache_prompt": True,
            # Belt and braces. <|im_end|> is marked EOG in these GGUFs, so
            # generation ends on the token; the string stop covers a build that
            # reads the metadata differently, and llama.cpp trims it either way.
            "stop": [STOP_TOKEN],
        }
        parts: list[str] = []
        stopped = False
        headers = {"Accept": "text/event-stream"}
        async with self._http().stream("POST", "/completion", json=payload, headers=headers, timeout=600.0) as response:
            if response.status_code != 200:
                raise RuntimeError(_error_text((await response.aread()).decode("utf-8", "replace")))
            async for raw in response.aiter_lines():
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                if line.startswith("error:"):
                    raise RuntimeError(_error_text(line[6:]))
                if not line.startswith("data:"):
                    continue
                message = json.loads(line[5:])
                if message.get("error"):
                    raise RuntimeError(_error_text(json.dumps(message["error"])))
                content = message.get("content") or ""
                if content:
                    parts.append(content)
                if message.get("stop"):
                    # Newer builds report `stop_type`; older ones report the
                    # three booleans. Either way the question is the same one:
                    # did it end, or did it run out of budget?
                    stop_type = message.get("stop_type")
                    if stop_type is not None:
                        stopped = stop_type in ("eos", "word")
                    else:
                        stopped = bool(message.get("stopped_eos") or message.get("stopped_word"))
        return "".join(parts), stopped


class ModelHost:
    """The current child, and the only thing allowed to replace it.

    THE LOCK GUARDS THE SWAP, NOT THE GENERATION. Callers take a reference to
    the running server through :meth:`acquire` and then talk to it without
    holding anything, which is what makes concurrent paragraph rewrites
    concurrent. A swap waits for the in-flight count to reach zero before it
    kills anything, so a rewrite in progress is never cut off by someone
    changing the selector in Settings.
    """

    def __init__(self, *, slots: int = DEFAULT_SLOTS) -> None:
        self.slots = slots
        self.state = "idle"  # idle | loading | ready | failed
        self.error = ""
        self.variant: Variant | None = None
        self.gpu = True
        self.server: LlamaServer | None = None
        self._lock = asyncio.Lock()
        self._inflight = 0
        self._idle = asyncio.Condition()
        self._stale = False
        self._idle_task: asyncio.Task | None = None
        self._last_used = time.monotonic()

    # ── loading ──────────────────────────────────────────────────────────────

    def mark_stale(self, variant: Variant | None, gpu: bool) -> None:
        """Record a new selection without touching the running child.

        The config route calls this and returns immediately: a turn may be
        mid-rewrite, and a settings write has no business blocking on it or
        killing it. The restart happens on the next :meth:`ensure`.
        """
        if self.variant is not None and variant is not None and self.variant.id == variant.id and self.gpu == gpu:
            return
        self.variant, self.gpu, self._stale = variant, gpu, True

    @property
    def healthy(self) -> bool:
        return not self._stale and self.server is not None and self.server.alive and self.server.ready

    async def ensure(self, variant: Variant, gpu: bool) -> LlamaServer:
        """The running server for *variant*, starting or restarting as needed."""
        async with self._lock:
            same = self.variant is not None and self.variant.id == variant.id and self.gpu == gpu
            if same and self.healthy and self.server is not None:
                return self.server
            binary = runtime.find_binary()
            path = catalog.variant_path(variant)
            if not os.path.exists(path):
                raise RuntimeError(f"{variant.label} is not downloaded — {variant.local_name} is missing.")
            # The flag goes up BEFORE the drain, not after it: new work has to
            # stop arriving for the drain to end, and `state` is what callers
            # read to turn themselves away with a message.
            self.variant, self.gpu, self.state, self.error, self._stale = variant, gpu, "loading", "", False
            await self._drain()
            if self.server is not None:
                await self.server.stop()
                self.server = None
            logger.info("Loading %s (%d MB, %d slots, gpu=%s)…", variant.local_name, variant.size_mb, self.slots, gpu)
            server = LlamaServer(variant, binary, slots=self.slots, gpu=gpu)
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
            logger.info("Prose rewriter ready in %.1fs on 127.0.0.1:%d", time.monotonic() - server.started_at, server.port)
            return server

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
        executable, so deleting a variant or replacing the binary fails there
        with a bare sharing violation and no exit but restarting Orb; elsewhere
        the unlink succeeds and leaves the child serving weights that are gone.

        Drains first, so a rewrite in flight finishes rather than being cut off
        — the same courtesy ``ensure`` pays a variant swap; the lock holds new
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
        """Stop the child and the idle watcher. Registered in the app lifespan."""
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
        """Stop the child after IDLE_TIMEOUT at zero in-flight, freeing VRAM."""
        while True:
            await asyncio.sleep(min(30.0, max(5.0, IDLE_TIMEOUT / 4)))
            if self.server is None:
                return
            if self._inflight or time.monotonic() - self._last_used < IDLE_TIMEOUT:
                continue
            async with self._lock:
                if self._inflight or self.server is None:
                    continue
                if time.monotonic() - self._last_used < IDLE_TIMEOUT:
                    continue
                logger.info("Prose rewriter idle for %.0fs; unloading %s", IDLE_TIMEOUT, self.server.variant.local_name)
                await self.server.stop()
                self.server = None
                self.state = "idle"
                self._idle_task = None
                return

    # ── in-flight accounting ─────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def acquire(self):
        """Hold one in-flight slot for the duration of a rewrite."""
        async with self._idle:
            self._inflight += 1
        try:
            yield
        finally:
            async with self._idle:
                self._inflight -= 1
                self._last_used = time.monotonic()
                self._idle.notify_all()


#: One host per process. The rewriter is a single-user local feature and a
#: second resident model would double the VRAM for no gain.
HOST = ModelHost()
