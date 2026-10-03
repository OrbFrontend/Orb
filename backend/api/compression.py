"""Gzip whole 200 text responses using Starlette's compressor.

Skip compressed media, SSE and byte ranges; compressing a 206 response
would invalidate Content-Range offsets and break seeking.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import Message, Receive, Scope, Send

_COMPRESSIBLE_PREFIXES = ("text/", "application/json", "application/javascript", "image/svg+xml")


def compressible(status: int, headers: Headers) -> bool:
    """Whether a response with *status* and *headers* is worth gzipping."""
    if status != 200 or "content-range" in headers:
        return False
    content_type = headers.get("content-type", "")
    return content_type.startswith(_COMPRESSIBLE_PREFIXES) and not content_type.startswith("text/event-stream")


class _TextGZipResponder(GZipResponder):
    async def send_with_compression(self, message: Message) -> None:
        if message["type"] == "http.response.start" and not compressible(message["status"], Headers(raw=message["headers"])):
            # Starlette's own pass-through switch for SSE; setting it here routes
            # every other excluded response down the same untouched path.
            await super().send_with_compression(message)
            self.content_type_is_excluded = True
            return
        await super().send_with_compression(message)


class TextGZipMiddleware(GZipMiddleware):
    """``GZipMiddleware`` restricted to whole, text-like ``200`` responses."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and "gzip" in Headers(scope=scope).get("Accept-Encoding", ""):
            responder = _TextGZipResponder(self.app, self.minimum_size, compresslevel=self.compresslevel)
            await responder(scope, receive, send)
            return
        await self.app(scope, receive, send)
