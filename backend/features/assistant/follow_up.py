"""The last reply's working context, held for one follow-up so it extends the context the model already read."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from .contracts import Continuation

# The prefix cache's own anchor lasts an hour; past that the follow-up gains nothing over a replay.
_KEPT_SECONDS = 3600


@dataclass(frozen=True)
class _Kept:
    token: str
    continuation: Continuation
    # The data version once the run settled, or ``None`` when another connection committed while it ran.
    version: str | None
    at: float


_kept: _Kept | None = None


def keep_follow_up(continuation: Continuation, version: str | None) -> str:
    """Hold *continuation* in place of any earlier one, and the token the browser sends back to resume it."""
    global _kept
    _kept = _Kept(secrets.token_hex(16), continuation, version, time.monotonic())
    return _kept.token


def take_follow_up(token: str, version: str | None) -> tuple[Continuation | None, bool]:
    """The held continuation *token* names, and whether nothing was committed since; every run takes it, matched or not.

    One slot, taken by whichever request comes next: a request from another tab, or after a Clear, sends no matching token and
    replays its own transcript.
    """
    global _kept
    kept, _kept = _kept, None
    if not token or kept is None or not secrets.compare_digest(kept.token, token):
        return None, False
    if time.monotonic() - kept.at > _KEPT_SECONDS:
        return None, False
    return kept.continuation, version is not None and kept.version == version
