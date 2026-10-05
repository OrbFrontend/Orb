"""Hold every request behind the access password, answering strangers with one anonymous sign-in page."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..database import get_access_password
from ..features import access

SESSION_COOKIE = "session"
_SESSION_MAX_AGE = 400 * 24 * 3600
_MAX_FORM_BYTES = 4096

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
</style>
</head>
<body>
<form method="post">
<input type="password" name="password" placeholder="Password" aria-label="Password" autocomplete="current-password" required autofocus>
NOTICE<button>Sign in</button>
</form>
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
        if lock is None or access.session_valid(lock, HTTPConnection(scope).cookies.get(SESSION_COOKIE)):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        notice = ""
        if scope["method"] == "POST" and Headers(scope=scope).get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            client = scope["client"][0] if scope.get("client") else ""
            if not self.throttle.attempt(client, time.monotonic()):
                notice = "Too many attempts. Wait a minute and try again."
            elif await asyncio.to_thread(access.password_matches, lock, await _posted_password(receive)):
                self.throttle.succeeded(client)
                response = RedirectResponse("/", status_code=303)
                set_session_cookie(response, scope, lock["session_key"])
                await response(scope, receive, send)
                return
            else:
                notice = "Wrong password."
        page = _PAGE.replace("NOTICE", f'<p role="alert">{notice}</p>\n' if notice else "")
        await HTMLResponse(page, status_code=401, headers={"Cache-Control": "no-store"})(scope, receive, send)


def set_session_cookie(response: Response, scope: Scope, session_key: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_key,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=scope.get("scheme") == "https",
    )


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
