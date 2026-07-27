"""Per-invocation observability, aggregated per extension.

This answers "which extension slows my turns", which is the first scaling
pressure point the design expects: each pre-hook may spend two serial model
calls before the Writer starts, and nothing else in the system would say so.

It is also the measurement a future per-turn aggregate pre-hook budget would be
set against. That budget is deliberately not enforced here -- limits get
widened or added against observed data, not against a guess about what a
package might do.

**Never package-visible.** Nothing in this module is projected into
``ExtensionCtx``, a flow value, a returned error, or a view's data. A flow that
could read its own timings would have a side channel and, worse, a reason to
branch on how fast the host is. The aggregate reaches exactly one place: the
manager's diagnostics, beside load status.

In-memory and process-local. These are operational counters, not an audit log;
persisting them would make a diagnostic into a data-retention question.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = Literal["ok", "error", "cancelled"]

MAX_TRACKED_EXTENSIONS = 256
"""Ceiling on the aggregate table. Bounded by installed packages in practice;
the cap keeps a pathological id churn from growing the dict without limit."""


@dataclass(slots=True)
class _Aggregate:
    invocations: int = 0
    errors: int = 0
    cancellations: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    model_calls: int = 0
    http_requests: int = 0
    last_outcome: Outcome | None = None
    last_entry_point: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "invocations": self.invocations,
            "errors": self.errors,
            "cancellations": self.cancellations,
            "average_ms": round(self.total_ms / self.invocations, 1) if self.invocations else 0.0,
            "max_ms": round(self.max_ms, 1),
            "model_calls": self.model_calls,
            "http_requests": self.http_requests,
            "last_outcome": self.last_outcome,
            "last_entry_point": self.last_entry_point,
        }


_AGGREGATES: dict[str, _Aggregate] = {}


@dataclass(slots=True)
class InvocationTimer:
    """Times one invocation and folds it into the extension's aggregate.

    Started by the adapter around the whole prepare/execute/commit span, so the
    recorded duration is what the *turn* paid, not what the interpreter spent
    executing steps -- lock waits and commits are part of the cost a user feels.
    """

    extension_id: str
    entry_point: str
    started: float = field(default_factory=time.perf_counter)

    def finish(self, outcome: Outcome, quotas: Mapping[str, int] | None = None) -> None:
        record(
            self.extension_id,
            entry_point=self.entry_point,
            outcome=outcome,
            duration_ms=(time.perf_counter() - self.started) * 1000.0,
            quotas=quotas or {},
        )


def record(
    extension_id: str,
    *,
    entry_point: str,
    outcome: Outcome,
    duration_ms: float,
    quotas: Mapping[str, int],
) -> None:
    aggregate = _AGGREGATES.get(extension_id)
    if aggregate is None:
        if len(_AGGREGATES) >= MAX_TRACKED_EXTENSIONS:
            return
        aggregate = _AGGREGATES.setdefault(extension_id, _Aggregate())
    aggregate.invocations += 1
    aggregate.total_ms += duration_ms
    aggregate.max_ms = max(aggregate.max_ms, duration_ms)
    aggregate.model_calls += int(quotas.get("model_calls", 0))
    aggregate.http_requests += int(quotas.get("http_requests", 0))
    aggregate.last_outcome = outcome
    aggregate.last_entry_point = entry_point
    if outcome == "error":
        aggregate.errors += 1
    elif outcome == "cancelled":
        aggregate.cancellations += 1


def summary(extension_id: str) -> dict[str, Any] | None:
    """The manager's diagnostics row, or ``None`` before the first invocation."""
    aggregate = _AGGREGATES.get(extension_id)
    return aggregate.as_dict() if aggregate is not None else None


def forget(extension_id: str) -> None:
    """Drop an uninstalled extension's counters."""
    _AGGREGATES.pop(extension_id, None)


def reset() -> None:
    """Clear every aggregate. For tests and process-level teardown."""
    _AGGREGATES.clear()
