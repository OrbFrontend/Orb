"""Limit how fast one client can guess the access password."""

from __future__ import annotations


class LoginThrottle:
    def __init__(self, attempts: int = 5, window: float = 60.0) -> None:
        self.attempts = attempts
        self.window = window
        self._counts: dict[str, tuple[int, float]] = {}

    def attempt(self, client: str, now: float) -> bool:
        count, since = self._counts.get(client, (0, now))
        if now - since >= self.window:
            count, since = 0, now
        if count >= self.attempts:
            return False
        self._counts[client] = (count + 1, since)
        if len(self._counts) > 1024:
            self._counts = {c: v for c, v in self._counts.items() if now - v[1] < self.window}
        return True

    def succeeded(self, client: str) -> None:
        self._counts.pop(client, None)
