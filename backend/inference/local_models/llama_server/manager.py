"""Every live host, for the two operations that touch all of them at once.

A host owns one resident model and belongs to its feature; this registry is not
a second owner. It exists because two things are properties of the *binary*
rather than of any one feature — stopping every child when Orb exits, and
letting go of every child before the executable underneath them is replaced —
and neither can be done from a module that only knows about one host.

A PLAIN LIST, NOT A WeakSet: hosts are module-level singletons that must not
become collectable while a child of theirs is running. ``register`` is
idempotent on identity.

Registration happens when the owning feature's service module is imported, so
``shutdown_all()`` is a no-op in a process that never imported one — which is
correct, because such a process cannot have spawned a child.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the host imports this module at runtime; this is the other half
    from .host import ManagedLlamaServerHost

logger = logging.getLogger(__name__)

_HOSTS: list[ManagedLlamaServerHost] = []


def register(host: ManagedLlamaServerHost) -> ManagedLlamaServerHost:
    if not any(h is host for h in _HOSTS):
        _HOSTS.append(host)
    return host


def unregister(host: ManagedLlamaServerHost) -> None:
    """Drop a host from the registry. For tests that build a throwaway one."""
    for i, h in enumerate(_HOSTS):
        if h is host:
            del _HOSTS[i]
            return


def hosts() -> tuple[ManagedLlamaServerHost, ...]:
    return tuple(_HOSTS)


async def _all(action: str) -> None:
    """Run *action* on every host, concurrently, and never skip one.

    CONCURRENTLY IS NOT PREMATURE: ``release`` drains with a 120 s ceiling, and
    serialising N of those would put N x 120 s between the user pressing Fetch
    and the binary being replaced, or between SIGINT and the process exiting.
    One host's failure must not leave another's child running, so exceptions
    are gathered and logged rather than raised.
    """
    targets = hosts()
    if not targets:
        return
    results = await asyncio.gather(*(getattr(h, action)() for h in targets), return_exceptions=True)
    for host, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("llama-server host %r failed to %s", host.name, action, exc_info=result)


async def release_all() -> None:
    """Every host lets go of the files its child holds; each reloads lazily."""
    await _all("release")


async def shutdown_all() -> None:
    """Every child stopped. The app lifespan's teardown — without it an orphan
    keeps the model resident and holds the GPU after Orb exits."""
    await _all("shutdown")
