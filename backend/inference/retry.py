"""Retry policy for transient LLM transport errors."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

# Temporary server-side HTTP statuses. 429/503/529 are explicit "busy/overloaded"
# signals; 408/500/502/504 are transient often enough on LLM backends (request
# timeouts, OOM/CUDA hiccups, gateway blips) to be worth a bounded retry.
# Client-side 4xx (400/401/404/422 ...) are deterministic and never retried.
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504, 529})

# Connection-level failures that mean the server was briefly unreachable rather
# than that the request was bad: a refused or timed-out connect, a read/protocol
# error or dropped connection before any response, or no free pool slot. Write-side
# and local/proxy protocol errors are excluded -- those are our fault, not a
# transient server blip. These only ever surface pre-stream, so retrying them is
# subject to the same "no event yet" guard as a status-code failure.
RETRYABLE_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


@dataclass(frozen=True)
class RetryPolicy:
    """When and how to retry a completion that failed with a transient error.

    ``count`` is the number of *retries* after the initial attempt, so at most
    ``1 + count`` requests and ``count`` waits of ``delay`` seconds. ``count=0``
    disables retrying.
    """

    count: int = 4
    delay: float = 5.0
    status_codes: frozenset[int] = RETRYABLE_STATUS

    def should_retry(self, exc: BaseException) -> bool:
        """True if *exc* is a transient failure worth retrying under this policy."""
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in self.status_codes
        return isinstance(exc, RETRYABLE_TRANSPORT_ERRORS)
