"""
retry.py -- transient-error retry policy for the LLM transport.

A completion that dies with a temporary server-side error (a 503, an overloaded
529, a refused or dropped connection) otherwise wastes the whole turn's compute,
and the later the pass fails the more is thrown away. :class:`RetryPolicy` lets
:meth:`LLMClient.complete` re-issue such a request a bounded number of times with
a fixed delay.

This is transport config, injected like ``timeout``/``completion_mode``; the
inference layer never reads settings itself (that would couple it to the database
layer), so callers build a policy from a settings mapping via
:meth:`RetryPolicy.from_settings` and pass it to the client constructor.

Retrying is only safe before the first streamed event -- once content has been
emitted, re-issuing would double it. That guard lives in
:meth:`LLMClient.complete`, not here; this module only decides *whether* an error
is retryable and *how long* to wait.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

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
    ``1 + count`` requests and ``count`` waits of ``delay`` seconds. The default
    instance is disabled: constructing an :class:`LLMClient` without a policy
    behaves exactly as before -- one attempt, error propagates.
    """

    enabled: bool = False
    count: int = 10
    delay: float = 5.0
    status_codes: frozenset[int] = RETRYABLE_STATUS

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "RetryPolicy":
        """Build a policy from a settings row.

        Missing or malformed values degrade to a safe disabled/no-op shape rather
        than raising on the gameplay path (an old DB predating the columns, a
        null slipping through).
        """
        return cls(
            enabled=bool(settings.get("retry_enabled", 0)),
            count=max(0, int(settings.get("retry_count", 10) or 0)),
            delay=max(0.0, float(settings.get("retry_delay_seconds", 5) or 0)),
        )

    def should_retry(self, exc: BaseException) -> bool:
        """True if *exc* is a transient failure worth retrying under this policy."""
        if not self.enabled:
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in self.status_codes
        return isinstance(exc, RETRYABLE_TRANSPORT_ERRORS)
