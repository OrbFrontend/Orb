"""Refuse requests a hostile web page could make through a visitor's browser.

Sits inside the access gate, so a locked server keeps answering strangers with its one anonymous page. On an open server a
Host that a rebinding page could carry is refused; a signed-in browser has already proved itself, which keeps reverse-proxy
domains working once a password is set. State-changing requests from another page are refused either way.
"""

from __future__ import annotations

import logging
import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..database import get_access_password
from ..features import access

logger = logging.getLogger(__name__)

ALLOWED_HOSTS_ENV = "ORB_ALLOWED_HOSTS"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Each refused name is logged once per process, so a page retrying in a loop cannot flood the log.
_reported: set[str] = set()


class RequestGuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        allowed = access.parse_allowed_hosts(os.environ.get(ALLOWED_HOSTS_ENV, ""))
        host = headers.get("host")
        if not access.rebind_safe_host(host, allowed) and await get_access_password() is None:
            _report(f"host {host}", "Refused a request for unlisted name %r on a server without a password", host)
            await _host_refusal(host or "")(scope, receive, send)
            return
        if scope["method"] not in _SAFE_METHODS:
            origin = headers.get("origin")
            if not access.same_origin_write(
                origin, headers.get("sec-fetch-site"), host, headers.get("x-forwarded-host"), allowed
            ):
                _report(f"origin {origin}", "Refused a write to %r from another site: Origin %r", host, origin)
                await _origin_refusal()(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _report(key: str, message: str, *args: object) -> None:
    if key in _reported or len(_reported) >= 256:
        return
    _reported.add(key)
    logger.warning(message, *args)


def _host_refusal(host: str) -> PlainTextResponse:
    name = access.host_name(host) if host else ""
    return PlainTextResponse(
        f'This server answers only to its own addresses, and "{name}" is not one of them.\n\n'
        "Open it by IP address, as localhost, by machine name or by Tailscale name instead.\n"
        "To use this name, set a password in Settings -> Password, or start the server with "
        f"{ALLOWED_HOSTS_ENV}={name}.\n",
        status_code=403,
    )


def _origin_refusal() -> JSONResponse:
    return JSONResponse(
        {
            "detail": "Refused a change requested by another site. If you reach Orb through a reverse proxy, have it forward "
            f"the Host header, or add this site's name to {ALLOWED_HOSTS_ENV}."
        },
        status_code=403,
    )
