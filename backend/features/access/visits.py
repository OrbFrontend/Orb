"""Sign-in passes: each one opens Orb's page once, then keeps serving that page's requests."""

from __future__ import annotations

import math
import secrets


class VisitPasses:
    def __init__(self, limit: int = 64, reload_window: float = 10.0) -> None:
        self.limit = limit
        self.reload_window = reload_window
        self._open_until: dict[str, float] = {}

    def issue(self, *, opened: bool) -> str:
        token = secrets.token_urlsafe(32)
        self._open_until[token] = -math.inf if opened else math.inf
        while len(self._open_until) > self.limit:
            del self._open_until[next(iter(self._open_until))]
        return token

    def valid(self, token: str | None) -> bool:
        return token is not None and token in self._open_until

    def open_page(self, token: str | None, now: float) -> bool:
        if token is None or self._open_until.get(token, -math.inf) < now:
            return False
        self._open_until[token] = -math.inf
        return True

    def allow_reload(self, token: str | None, now: float) -> None:
        if token is not None and token in self._open_until:
            self._open_until[token] = now + self.reload_window

    def clear(self) -> None:
        self._open_until.clear()
