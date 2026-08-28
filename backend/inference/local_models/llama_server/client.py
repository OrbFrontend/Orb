"""One llama-server child, and the three endpoints a caller asks it for.

WHY A CHILD PROCESS RATHER THAN A BINDING. A feature that wants several
requests decoded *together* needs continuous batching. Reaching it through
``llama-cpp-python`` (the binding the in-process classifiers use) means driving
``llama_decode`` and the KV cache by hand from Python; reaching it through
``llama-server`` means ``--parallel N --cont-batching`` and an HTTP call. There
is no third option that is less work than the second — which is also why a
``llama_server`` feature needs none of ``requirements-ml.txt`` except
``huggingface_hub`` for the download.

THE CHILD IS BOUND TO LOOPBACK ON AN EPHEMERAL PORT and is not the thing anyone
connects to. Its own web UI is off, and nothing here exposes the port, because
it has no authentication and ``/slots`` will hand out other people's prompts.

:class:`LaunchProfile` lives here rather than in :mod:`host` because ``_argv``
is what reads nearly every field of it, and ``host`` imports this module — the
other direction would be a cycle for one dataclass. It is the whole command
line, and every number on it is an int this process owns.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import binary as binary_module
from .process import Child, spawn

logger = logging.getLogger(__name__)

BOOT_TIMEOUT = 300.0


@dataclass(frozen=True)
class LaunchProfile:
    """Everything one child is started with, and its identity as a load key.

    THE NUMBERS ARE CODE-OWNED, and ``__post_init__`` says so at runtime. A
    persisted or request-supplied value must first resolve through the calling
    feature's own closed allowlist; what may reach argv is the literal that
    comes back out of it, never the value that came in. That is the barrier
    CodeQL asks for at a subprocess sink, and range-checking an int and passing
    the original through would leave the taint attached even where the range is
    safe.

    Equality is the load key: two profiles built from the same selection
    compare equal, and any change of model, placement or lane count does not.
    ``ManagedLlamaServerHost`` swaps on exactly that comparison.
    """

    model_id: str  # opaque identity for the caller's own registry
    model_path: str  # trusted absolute path, resolved by the caller's closed catalog
    alias: str
    gpu_layers: int  # 999 | 0 — an int chosen by the feature, never a settings string
    ctx_size: int
    parallel: int
    http_threads: int
    cont_batching: bool = True
    no_webui: bool = True  # asked for only if the binary says it knows the flag
    label: str = ""  # log text
    size_mb: int = 0  # log text

    def __post_init__(self) -> None:
        # Every argv-bound number is an int owned by this process, not a value
        # that arrived over HTTP. type() is exact on purpose: bool is an int.
        for value in (self.gpu_layers, self.ctx_size, self.parallel, self.http_threads):
            if type(value) is not int:
                raise TypeError("launch profile numbers must be code-owned ints")


def _argv(profile: LaunchProfile, binary: Path, port: int) -> list[str]:
    """The child's command line, read off the profile and nothing else."""
    argv = [
        str(binary),
        "--model",
        profile.model_path,
        "--alias",
        profile.alias,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        # GPU vs CPU is this flag ALONE. Vulkan is a property of which binary
        # was fetched, not a runtime switch.
        "--n-gpu-layers",
        str(profile.gpu_layers),
        "--ctx-size",
        str(profile.ctx_size),
        "--parallel",
        str(profile.parallel),
    ]
    if profile.cont_batching:
        argv.append("--cont-batching")
    argv += ["--threads-http", str(profile.http_threads)]
    # Optional, and asked for only if this build has it: nothing here calls
    # /v1/chat/completions, and llama.cpp's own front end has no business being
    # reachable on a port we opened. Read through the module so the probe stays
    # one patchable seam.
    if profile.no_webui and binary_module.supports_flag(binary, "--no-webui"):
        argv.append("--no-webui")
    return argv


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


class LlamaServerClient:
    """A running child, and the three endpoints a caller asks it for:
    ``/health`` while it loads, ``/tokenize`` to size a job, ``/completion``
    to run one."""

    def __init__(self, profile: LaunchProfile, binary: Path) -> None:
        self.profile = profile
        self.binary = binary
        self.slots = profile.parallel
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
        self.argv = _argv(profile, binary, self.port)

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
                model = os.path.basename(self.profile.model_path)
                raise RuntimeError(f"llama-server exited with status {code} while loading {model}:\n{self.tail()}")
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

    async def generate(
        self,
        prompt: str,
        *,
        n_predict: int,
        temperature: float,
        top_p: float,
        stop: Sequence[str] = (),
        cache_prompt: bool = True,
    ) -> tuple[str, bool]:
        """Stream one completion; return ``(text, stopped)``.

        ``stopped`` is whether the model ended the generation itself; a caller
        trims the half-sentence tail of one that merely ran out of budget.
        Cancelling the awaiting task closes the connection mid-stream, which
        llama.cpp treats as a cancellation, so Stop frees the slot at once.

        *stop* belongs to the CALLER'S WEIGHTS, not to llama-server: a stop
        token is a property of the checkpoint's chat template. The key is
        omitted entirely when nothing was asked for, so a caller with no stop
        sequence sends the body a stop-less client would have sent.
        """
        payload: dict = {
            "prompt": prompt,
            "n_predict": n_predict,
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,
            "cache_prompt": cache_prompt,
        }
        if stop:
            payload["stop"] = list(stop)
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
