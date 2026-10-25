"""Set or remove the access password."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request, Response

from ...database import clear_access_password, get_access_password, set_access_password
from ...features import access
from ..access_gate import SESSION_COOKIE, grant_session, visit_passes
from ..schemas import AccessPasswordUpdate

router = APIRouter()


@router.get("/api/access")
async def api_get_access():
    return {"password_set": await get_access_password() is not None}


@router.put("/api/access/password")
async def api_set_access_password(body: AccessPasswordUpdate, request: Request, response: Response):
    visit_passes.clear()
    if not body.password:
        await clear_access_password()
        return {"password_set": False}
    await set_access_password(await asyncio.to_thread(access.new_lock, body.password))
    grant_session(response, request.scope, page_open=True)
    return {"password_set": True}


@router.post("/api/access/reload")
async def api_allow_reload(request: Request):
    visit_passes.allow_reload(request.cookies.get(SESSION_COOKIE), time.monotonic())
    return {"ok": True}
