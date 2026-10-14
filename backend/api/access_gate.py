"""Hold every request behind the access password, answering strangers with one anonymous sign-in page."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import parse_qs

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..database import get_access_password
from ..database.models import AccessPasswordRow
from ..features import access

SESSION_COOKIE = "session"
visit_passes = access.VisitPasses()
_MAX_FORM_BYTES = 4096
_PAGE_TYPES = ("text/html", "application/xhtml+xml")
_CONDITIONAL_HEADERS = (b"if-none-match", b"if-modified-since")

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Sign in</title>
<link rel="icon" href="data:,">
<style>
:root{color-scheme:light dark}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:Canvas;color:CanvasText;font:16px system-ui,sans-serif}
form{display:flex;flex-direction:column;gap:12px;width:min(320px,calc(100vw - 32px))}
input,button{font:inherit;padding:10px 12px;border-radius:6px}
input{border:1px solid GrayText;background:Field;color:FieldText}
button{border:0;background:CanvasText;color:Canvas;cursor:pointer}
p{margin:0;font-size:14px}
input:autofill{animation:filled 1ms}
@keyframes filled{from{}to{}}
</style>
</head>
<body>
<form method="post">
<input type="password" name="password" placeholder="Password" aria-label="Password" autocomplete="current-password" required autofocus>
NOTICE<button>Sign in</button>
</form>
<script>
const input = document.querySelector("input");
input.addEventListener("animationstart", () => input.select());
</script>
</body>
</html>
"""


class AccessGateMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.throttle = access.LoginThrottle()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        lock = await get_access_password()
        if lock is None:
            await self.app(scope, receive, send)
            return
        if _is_sign_in(scope):
            await self._sign_in(lock, scope, receive, send)
            return
        cookie = HTTPConnection(scope).cookies.get(SESSION_COOKIE)
        if visit_passes.valid(cookie):
            await self.app(_uncached_navigation(scope), receive, _open_pages_once(scope, receive, send, cookie))
        elif scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await _sign_in_page("")(scope, receive, send)

    async def _sign_in(self, lock: AccessPasswordRow, scope: Scope, receive: Receive, send: Send) -> None:
        client = scope["client"][0] if scope.get("client") else ""
        if not self.throttle.attempt(client, time.monotonic()):
            notice = "Too many attempts. Wait a minute and try again."
        elif await asyncio.to_thread(access.password_matches, lock, await _posted_password(receive)):
            self.throttle.succeeded(client)
            response = RedirectResponse("/", status_code=303)
            grant_session(response, scope, page_open=False)
            await response(scope, receive, send)
            return
        else:
            notice = "Wrong password."
        await _sign_in_page(notice)(scope, receive, send)


def _is_sign_in(scope: Scope) -> bool:
    return (
        scope["type"] == "http"
        and scope["method"] == "POST"
        and Headers(scope=scope).get("content-type", "").startswith("application/x-www-form-urlencoded")
    )


def _sign_in_page(notice: str) -> Response:
    page = _PAGE.replace("NOTICE", f'<p role="alert">{notice}</p>\n' if notice else "")
    return HTMLResponse(page, status_code=401, headers={"Cache-Control": "no-store"})


def grant_session(response: Response, scope: Scope, *, page_open: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        visit_passes.issue(opened=page_open),
        httponly=True,
        samesite="lax",
        secure=scope.get("scheme") == "https",
    )


def _uncached_navigation(scope: Scope) -> Scope:
    if scope["type"] != "http" or Headers(scope=scope).get("sec-fetch-mode", "navigate") != "navigate":
        return scope
    return {**scope, "headers": [(k, v) for k, v in scope["headers"] if k not in _CONDITIONAL_HEADERS]}


def _open_pages_once(scope: Scope, receive: Receive, send: Send, token: str | None) -> Send:
    refused = False

    async def send_page_once(message: Message) -> None:
        nonlocal refused
        if refused:
            return
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            if headers.get("content-type", "").startswith(_PAGE_TYPES):
                if not visit_passes.open_page(token):
                    refused = True
                    await _sign_in_page("")(scope, receive, send)
                    return
                headers["Cache-Control"] = "no-store"
        await send(message)

    return send_page_once


async def _posted_password(receive: Receive) -> str:
    body = b""
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            return ""
        body += message.get("body", b"")
        if len(body) > _MAX_FORM_BYTES:
            return ""
        more = message.get("more_body", False)
    return parse_qs(body.decode("latin-1")).get("password", [""])[0]
