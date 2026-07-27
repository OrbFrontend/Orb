"""Staging tokens: the binding between what a user was shown and what installs.

Inspection and installation are two requests. Between them the user reads a
consent diff. The token is what makes the second request refer to the *same*
package the first one described -- it carries the digest, the operation, the
extension id, the derived requirement set, and the active digest observed at
inspection time, so installation never has to re-derive any of that from a
request body the frontend assembled.

That inversion is the security property. The frontend sends an opaque token
plus the exact normalized grants the user approved; it never reconstructs a
permission from a display string, and it cannot name a digest the server did
not just compile and show.

Tokens are:

* **opaque** -- 256 bits from ``secrets.token_urlsafe``, no structure to forge;
* **short-lived** -- :data:`TOKEN_TTL_SECONDS`, long enough to read a consent
  screen and short enough that a stale tab cannot install tomorrow's decision;
* **single-use** -- redeeming consumes it, so a replayed install request fails
  rather than re-running a lifecycle mutation;
* **in-memory** -- deliberately invalid after a restart, because the thing a
  token asserts is "a human just looked at this", which does not survive a
  process the user was not watching.

The package *content* is not held here, and neither is the permission list the
user was shown. Inspection materializes the files into the content store, and
redemption recompiles from the store and revalidates the digest -- which proves
the manifest is byte-identical to the one that produced the consent screen, so
a second copy of its requests would only be something to keep in sync.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

TOKEN_TTL_SECONDS = 900.0
"""Fifteen minutes. A consent screen that has been open longer is re-inspected
rather than trusted -- for an update, the active digest it diffed against may
no longer be the installed one."""

MAX_PENDING_TOKENS = 32
"""Bound on concurrently staged inspections, so a client that inspects in a loop
cannot grow this table without limit. Expired entries are swept first; only
genuinely live tokens count against it."""

Operation = Literal["install", "update", "rollback", "purge"]


@dataclass(frozen=True, slots=True)
class StagedOperation:
    """One inspected, not-yet-applied lifecycle operation.

    ``observed_active_digest`` is what the package's active digest was when the
    diff was computed -- ``""`` for a fresh install. Redemption compares it
    against the live value and refuses on a mismatch, which is what turns "the
    user approved an update from A to B" into a claim that is still true at
    commit time rather than one that was true when the page rendered.

    ``payload`` carries operation-specific detail the route needs to apply the
    decision (a purge preview's counts, an update's source). It never carries
    package bytes.
    """

    token: str
    operation: Operation
    extension_id: str
    digest: str
    observed_active_digest: str
    expires_at: float
    payload: dict[str, Any] = field(default_factory=dict)

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at


class StagingError(Exception):
    """A token is unknown, expired, already used, or for the wrong operation.

    One exception for all four: the caller maps it to a single "re-inspect and
    try again" response, and distinguishing them for the client would let a
    caller probe which tokens exist.
    """


_PENDING: dict[str, StagedOperation] = {}


def stage(
    *,
    operation: Operation,
    extension_id: str,
    digest: str,
    observed_active_digest: str = "",
    payload: dict[str, Any] | None = None,
) -> StagedOperation:
    """Register a staged operation and return it, token included."""
    _sweep()
    if len(_PENDING) >= MAX_PENDING_TOKENS:
        # Drop the oldest live token rather than refusing the new inspection:
        # the user is looking at the screen they just opened, not the one they
        # abandoned twenty inspections ago.
        oldest = min(_PENDING.values(), key=lambda entry: entry.expires_at)
        _PENDING.pop(oldest.token, None)
    staged = StagedOperation(
        token=secrets.token_urlsafe(32),
        operation=operation,
        extension_id=extension_id,
        digest=digest,
        observed_active_digest=observed_active_digest,
        expires_at=time.monotonic() + TOKEN_TTL_SECONDS,
        payload=dict(payload or {}),
    )
    _PENDING[staged.token] = staged
    return staged


def redeem(
    token: object,
    *,
    operation: Operation | Sequence[Operation],
    extension_id: str | None = None,
) -> StagedOperation:
    """Consume a token, or raise :class:`StagingError`.

    Single-use is enforced by removing the entry *before* any validation of its
    contents, so a request that fails the operation or id check still burns the
    token. A token that could be retried after a rejection would let a client
    hunt for the operation it was minted for.

    *operation* accepts several values because update and rollback share one
    apply path -- they differ only in where the bytes came from. Passing the
    whole accepted set is deliberate rather than calling twice: a caller that
    tried one operation and then the other would burn the token on the first
    attempt and never reach the second.
    """
    _sweep()
    accepted = (operation,) if isinstance(operation, str) else tuple(operation)
    if not isinstance(token, str) or not token:
        raise StagingError("a staging token is required; inspect the package first")
    staged = _PENDING.pop(token, None)
    if staged is None or staged.expired():
        raise StagingError("this staging token is unknown or has expired; inspect the package again")
    if staged.operation not in accepted:
        raise StagingError("this staging token was issued for a different operation")
    if extension_id is not None and staged.extension_id != extension_id:
        raise StagingError("this staging token was issued for a different extension")
    return staged


def discard(extension_id: str) -> None:
    """Invalidate every pending token for one extension.

    Called after any lifecycle mutation: every open consent screen for that
    package described a state that no longer exists, and the digest check would
    reject them one request later anyway. Doing it here means the failure is
    "re-inspect", not "409 on the thing you just did".
    """
    for token, staged in list(_PENDING.items()):
        if staged.extension_id == extension_id:
            del _PENDING[token]


def clear() -> None:
    """Drop every pending token. Shutdown, reset, and test isolation."""
    _PENDING.clear()


def pinned_digests() -> set[str]:
    """Package digests still named by live inspection/consent tokens."""
    _sweep()
    return {staged.digest for staged in _PENDING.values() if staged.digest}


def _sweep() -> None:
    now = time.monotonic()
    for token, staged in list(_PENDING.items()):
        if staged.expired(now):
            del _PENDING[token]
