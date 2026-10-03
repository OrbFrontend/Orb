"""
Integration test fixtures.

Strategy:
- Patch backend.database.connection.DB_PATH to a per-test temp file before any DB call.
- Seed that file by copying a session-scoped template built once by init_db()
  (bypasses FastAPI lifespan, which ASGITransport does not trigger).
- Yield an httpx.AsyncClient wired to the real ASGI app.
- Yield a raw aiosqlite connection for direct DB assertions.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
from pathlib import Path

import aiosqlite
import httpx
import pytest
import uvicorn
from httpx import ASGITransport

import backend.database.connection as db_connection
from backend.database import init_db

from ._llm_mock import FakeLLMClient, llm_factory, verify_kv_prefix_invariants


@pytest.fixture(autouse=True)
def _reset_module_locks():
    """Clear process-global lock caches between fresh databases/event loops.

    Reused ids can otherwise find locks bound to a closed prior loop when contended.
    workflow_config_lock already keys by loop and needs no reset.
    """
    from backend.api import deps
    from backend.core import locks

    lock_dicts = (
        deps._workflow_root_locks,
        deps._conversation_stream_locks,
        deps._active_streams,
        locks._workflow_state_locks,
        locks._workflow_character_state_locks,
    )
    for d in lock_dicts:
        d.clear()
    yield
    for d in lock_dicts:
        d.clear()


@pytest.fixture(scope="session")
def _fresh_db_template(tmp_path_factory, _never_the_real_database) -> Path:
    """A fresh-install database, built once and copied per test.

    ``init_db`` runs the whole CREATE TABLES script plus every seed insert. At
    ~0.6s a test that was the single largest line item in the suite, and it
    produces the same bytes every time, so it runs once here and each test
    copies the result.

    Tests that exercise ``init_db`` or the migration chain itself (e.g.
    ``test_fresh_install_stamping``) still call it directly and are unaffected.
    """
    template = tmp_path_factory.mktemp("db_template") / "template.db"

    async def _build() -> None:
        # `_never_the_real_database` is depended on above, not for a value but for
        # ordering: without it this can run first, and `original` is then the real
        # database path, which the restore below would reinstate for the whole session.
        original = db_connection.DB_PATH
        db_connection.DB_PATH = str(template)
        try:
            await init_db()
        finally:
            db_connection.DB_PATH = original

    asyncio.run(_build())
    return template


@pytest.fixture
async def db_path(tmp_path: Path, _fresh_db_template: Path) -> Path:
    """A per-test database file, pre-seeded from the session template."""
    path = tmp_path / "test.db"
    shutil.copyfile(_fresh_db_template, path)
    return path


@pytest.fixture
async def client(db_path: Path, monkeypatch):
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))

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
def llm_mock(monkeypatch, request):
    """Patch LLMClient at the shared factory seam for all production construction.

    Teardown checks captured KV prefixes; kv_divergence_expected opts out for
    intentional mid-conversation changes.
    """
    fake = FakeLLMClient()
    factory = llm_factory(fake)
    monkeypatch.setattr("backend.inference.client.LLMClient", factory)
    yield fake
    if request.node.get_closest_marker("kv_divergence_expected"):
        return
    violations = verify_kv_prefix_invariants(fake.captured)
    if violations:
        pytest.fail(
            "KV-cache prefix invariant violated (checked for every llm_mock test at teardown):\n"
            + "\n".join(f"  - {v}" for v in violations),
            pytrace=False,
        )


@pytest.fixture
async def streaming_client(db_path: Path, monkeypatch):
    """Provide real uvicorn loopback streaming for tests that gate in-flight events.

    ASGITransport buffers the full response and deadlocks such tests. Disable
    lifespan to match client; db_path supplies the schema.
    """
    monkeypatch.setattr(db_connection, "DB_PATH", str(db_path))

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
        # The app is SSE-based and uses no WebSocket endpoints. Disabling the
        # WebSocket protocol avoids uvicorn importing the deprecated
        # ``websockets.legacy`` module, which emits DeprecationWarnings.
        ws="none",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))

    async def _shutdown() -> None:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=2.0)
        except TimeoutError:
            # uvicorn's force_exit path skips the connection-drain polls
            # but the trailing server.wait_closed() call is not gated by
            # it. The second bounded wait gives uvicorn's own graceful
            # timeout a chance to fire; the explicit cancel covers the
            # case where even that path stalls.
            server.force_exit = True
            try:
                await asyncio.wait_for(serve_task, timeout=2.0)
            except TimeoutError:
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

        # httpx defaults to a 5s read timeout, which here is a stopwatch on
        # the *gap between SSE events* -- and every test on this fixture
        # deliberately parks the server mid-pipeline while it does something
        # else. Under `tests.sh all` (-n 8) that gap is scheduling noise, not
        # behaviour: the three tests on this fixture failed intermittently with
        # httpx.ReadTimeout on a loaded box while asserting nothing about
        # latency. Each one already bounds its own waits (gate events, the
        # ~2s lock-release poll), so the transport timeout is pure flake
        # surface; raise it to a value only a real hang can reach.
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=timeout) as ac:
            yield ac
    finally:
        await _shutdown()
