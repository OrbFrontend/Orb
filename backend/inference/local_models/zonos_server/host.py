"""Run the zonos3-server child under the shared server host: started on demand, stopped when idle."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..llama_server.host import ManagedServerHost
from ..llama_server.process import Child, free_port, spawn, stop
from . import binary

BOOT_TIMEOUT = 300.0


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


class ZonosServer:
    """One zonos3-server child."""

    def __init__(self, launch: Launch, executable: Path) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.argv = launch.argv(str(executable), self.port)
        self.started_at = time.monotonic()
        self.ready = False
        self.child: Child | None = None
        self.log: deque[str] = deque(maxlen=40)

    @property
    def alive(self) -> bool:
        return self.child is not None and self.child.returncode is None

    async def start(self) -> None:
        self.child = await spawn(self.argv, self.log.append)

    async def wait_ready(self) -> None:
        deadline = time.monotonic() + BOOT_TIMEOUT
        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.monotonic() < deadline:
                if self.child is None or self.child.returncode is not None:
                    await self.stop()
                    tail = "\n".join(self.log)[-600:] or "it exited"
                    raise ZonosServerFailed(f"The ZONOS3 server stopped while starting:\n{tail}")
                with contextlib.suppress(httpx.HTTPError):
                    if (await client.get(f"{self.url}/v2/capabilities")).status_code == 200:
                        self.ready = True
                        return
                await asyncio.sleep(0.25)
        await self.stop()
        raise ZonosServerFailed(f"The ZONOS3 server did not answer within {BOOT_TIMEOUT:.0f} s.")

    async def stop(self) -> None:
        self.ready = False
        child, self.child = self.child, None
        if child is not None:
            await stop(child)


class ZonosServerHost(ManagedServerHost[Launch, ZonosServer]):
    """One resident zonos3-server, restarted when its launch changes."""

    def _new_server(self, profile: Launch) -> ZonosServer:
        return ZonosServer(profile, binary.find_binary())
