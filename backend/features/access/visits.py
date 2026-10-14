"""Sign-in passes: each one opens Orb's page once, then keeps serving that page's requests."""

from __future__ import annotations

import secrets


class VisitPasses:
    def __init__(self, limit: int = 64) -> None:
        self.limit = limit
        self._opened: dict[str, bool] = {}

    def issue(self, *, opened: bool) -> str:
        token = secrets.token_urlsafe(32)
        self._opened[token] = opened
        while len(self._opened) > self.limit:
            del self._opened[next(iter(self._opened))]
        return token

    def valid(self, token: str | None) -> bool:
        return token is not None and token in self._opened

    def open_page(self, token: str | None) -> bool:
        if token is None or self._opened.get(token) is not False:
            return False
        self._opened[token] = True
        return True

    def clear(self) -> None:
        self._opened.clear()
