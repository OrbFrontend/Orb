"""Set default Cache-Control without replacing handler-provided headers.

API/SSE use no-store; static files use no-cache for ETag revalidation.
Plain ASGI avoids BaseHTTPMiddleware's response re-streaming overhead.
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
