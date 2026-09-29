"""Gzip for text responses: JSON, scripts, stylesheets and markup.

Starlette's ``GZipMiddleware`` compresses everything except SSE. That is wrong
for two kinds of response Orb serves:

* media that is already compressed (avatars, generated images, audio, woff2),
  where gzip spends CPU to make the body no smaller;
* a ``206`` byte range, where the ``Content-Range`` offsets name the
  uncompressed bytes, so a gzipped slice breaks audio seeking.

This keeps Starlette's compressor and narrows *what* it compresses: a whole
``200`` of a text-like type. Everything else passes through untouched.
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
