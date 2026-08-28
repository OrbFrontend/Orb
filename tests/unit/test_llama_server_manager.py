"""The registry of live hosts, and the two operations that touch all of them.

A host belongs to its feature; this registry exists only for what is true of
the *binary* — stopping every child when Orb exits, and letting go of every
child before the executable underneath them is replaced. Neither can be done
from a module that knows about one host.

The registry is process-global, so every test here builds its own hosts and
unregisters them rather than reaching into the module's list.
"""

from __future__ import annotations

import pytest

from backend.inference.local_models.llama_server import manager

pytestmark = pytest.mark.asyncio


class _FakeHost:
    def __init__(self, name: str, *, explodes: bool = False) -> None:
        self.name = name
        self.explodes = explodes
        self.released = False
        self.was_shut_down = False

    async def release(self) -> None:
        self.released = True
        if self.explodes:
            raise RuntimeError("release failed")

    async def shutdown(self) -> None:
        self.was_shut_down = True
        if self.explodes:
            raise RuntimeError("shutdown failed")


@pytest.fixture
def registered():
    """Register hosts for one test and take them back out again."""
    made: list[_FakeHost] = []

    def register(*hosts: _FakeHost) -> tuple[_FakeHost, ...]:
        for host in hosts:
            manager.register(host)
            made.append(host)
        return hosts

    yield register
    for host in made:
        manager.unregister(host)


async def test_release_all_releases_every_registered_host(registered):
    first, second = registered(_FakeHost("first"), _FakeHost("second"))

    await manager.release_all()

    assert first.released and second.released


async def test_shutdown_all_stops_every_registered_host(registered):
    first, second = registered(_FakeHost("first"), _FakeHost("second"))

    await manager.shutdown_all()

    assert first.was_shut_down and second.was_shut_down


async def test_one_failing_host_does_not_leave_another_child_running(registered):
    """An orphaned llama-server holds the GPU after Orb exits, so a raise from
    the first host may not skip the second."""
    broken, healthy = registered(_FakeHost("broken", explodes=True), _FakeHost("healthy"))

    await manager.shutdown_all()  # must not raise

    assert broken.was_shut_down and healthy.was_shut_down


async def test_registration_is_idempotent_on_identity(registered):
    (host,) = registered(_FakeHost("only"))
    manager.register(host)

    assert sum(1 for h in manager.hosts() if h is host) == 1


async def test_an_empty_registry_is_a_no_op():
    """A process that never imported a feature cannot have spawned a child, so
    the lifespan's teardown has nothing to do and must not care."""
    assert not [h for h in manager.hosts() if isinstance(h, _FakeHost)]
    await manager.release_all()
    await manager.shutdown_all()
