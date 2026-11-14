"""Track live child-process hosts for global lifecycle operations."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, TypeVar


class Host(Protocol):
    """A supervised child process the app releases and shuts down with the rest."""

    name: str

    async def release(self) -> None: ...

    async def shutdown(self) -> None: ...


logger = logging.getLogger(__name__)

_HOSTS: list[Host] = []
_H = TypeVar("_H", bound=Host)


def register(host: _H) -> _H:
    if not any(h is host for h in _HOSTS):
        _HOSTS.append(host)
    return host


def hosts() -> tuple[Host, ...]:
    return tuple(_HOSTS)


async def _all(action: str) -> None:
    """Run *action* on every host, concurrently, and never skip one.

    CONCURRENTLY IS NOT PREMATURE: ``release`` drains with a 120 s ceiling, and serialising N of those would put N x 120 s
    between the user pressing Fetch and the binary being replaced, or between SIGINT and the process exiting. One host's failure
    must not leave another's child running, so exceptions are gathered and logged rather than raised.
    """
    targets = hosts()
    if not targets:
        return
    results = await asyncio.gather(*(getattr(h, action)() for h in targets), return_exceptions=True)
    for host, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("local model host %r failed to %s", host.name, action, exc_info=result)


async def release_all() -> None:
    """Every host lets go of the files its child holds; each reloads lazily."""
    await _all("release")


async def shutdown_all() -> None:
    """Every child stopped. The app lifespan's teardown -- without it an orphan
    keeps the model resident and holds the GPU after Orb exits."""
    await _all("shutdown")
