"""
Integration test fixtures.

Strategy:
- Patch backend.database.connection.DB_PATH to a per-test temp file before any DB call.
- Call init_db() directly (bypasses FastAPI lifespan, which ASGITransport does not trigger).
- Yield an httpx.AsyncClient wired to the real ASGI app.
- Yield a raw aiosqlite connection for direct DB assertions.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import aiosqlite
import httpx
import pytest
import uvicorn
from httpx import ASGITransport

import backend.database.connection as db_connection
from backend.database import init_db

from ._llm_mock import FakeLLMClient, llm_factory


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
async def client(db_path: Path, monkeypatch):
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    await init_db()

    from backend.main import app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.fixture
async def db(db_path: Path):
    """Raw aiosqlite connection for post-call DB assertions."""
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


@pytest.fixture
def llm_mock(monkeypatch):
    """Substitute the streaming LLM client across every bound import.

    ``from .llm_client import LLMClient`` binds a local name at import
    time, so patching only ``backend.llm_client.LLMClient`` is not
    enough -- ``backend.main`` and ``backend.orchestrator`` retain
    pre-patch references. The fixture patches all three.
    """
    fake = FakeLLMClient()
    factory = llm_factory(fake)
    monkeypatch.setattr("backend.llm_client.LLMClient", factory)
    monkeypatch.setattr("backend.main.LLMClient", factory)
    monkeypatch.setattr("backend.orchestrator.LLMClient", factory)
    return fake


@pytest.fixture
async def streaming_client(db_path: Path, monkeypatch):
    """``httpx.AsyncClient`` against a real uvicorn loopback for tests that
    need server-sent events to actually stream chunk-by-chunk.

    The default ``client`` fixture wraps the app in ``ASGITransport``,
    which accumulates the entire response body before producing the
    ``Response`` object. Any test that pauses the server inside the
    response generator (e.g. by gating an LLM call) deadlocks under that
    transport because the client cannot enter the response body until the
    server has fully finished sending it. A real HTTP loopback restores
    incremental delivery so the test can observe a streamed event,
    interact with the server while the stream is still open, and then
    drive the stream to completion.

    Lifespan is disabled to match the existing ``client`` fixture, which
    drives ``init_db`` directly instead of through FastAPI's startup hook.
    """
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))
    await init_db()

    from backend.main import app

    # Bind the socket here (rather than letting uvicorn bind by host/port) so
    # the OS-assigned ephemeral port stays reserved across the handoff into
    # server.serve(sockets=[sock]); otherwise the window between
    # getsockname() and uvicorn's own bind would let another process grab it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    # timeout_graceful_shutdown=1 ensures uvicorn's internal wait_closed
    # path is bounded; without it, undrained client transports can pin
    # shutdown for the default 30s+ window. host/port are passed for log
    # clarity -- the sockets=[...] arg below is what governs binding.
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))

    async def _shutdown() -> None:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=2.0)
        except asyncio.TimeoutError:
            # uvicorn's force_exit path skips the connection-drain polls
            # but the trailing server.wait_closed() call is not gated by
            # it. The second bounded wait gives uvicorn's own graceful
            # timeout a chance to fire; the explicit cancel covers the
            # case where even that path stalls.
            server.force_exit = True
            try:
                await asyncio.wait_for(serve_task, timeout=2.0)
            except asyncio.TimeoutError:
                serve_task.cancel()
                await asyncio.gather(serve_task, return_exceptions=True)
        # uvicorn closes the socket itself on a normal exit; cover the
        # cancelled path where it never reaches that branch.
        try:
            sock.close()
        except OSError:
            pass

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not server.started:
            if loop.time() > deadline:
                await _shutdown()
                raise RuntimeError("uvicorn did not start within 5s")
            await asyncio.sleep(0.01)

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as ac:
            yield ac
    finally:
        await _shutdown()
