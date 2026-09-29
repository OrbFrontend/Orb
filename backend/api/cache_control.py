"""Default ``Cache-Control`` for every response a handler did not set one on.

API and SSE responses default to ``no-store``. A handler can opt into caching by
setting its own header first, as the avatar routes do, and that value is kept.
``/static`` defaults to ``no-cache`` instead: StaticFiles sends an ETag, so an
unchanged module costs a bodyless 304 rather than a full download on every
page load, and an edited one is still picked up by a reload.

Plain ASGI rather than ``BaseHTTPMiddleware``: that base class re-streams every
response through a memory channel, which measured ~180 us per request and
~39 us per SSE chunk here, against ~1 us for rewriting the start message.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class CacheControlMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        default = "no-cache" if scope["path"].startswith("/static/") else "no-store"

        async def send_with_default(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Cache-Control", default)
            await send(message)

        await self.app(scope, receive, send_with_default)
