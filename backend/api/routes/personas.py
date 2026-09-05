"""User-persona CRUD routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ...database import (
    create_user_persona,
    delete_user_persona,
    get_persona_avatar,
    get_user_personas,
    update_user_persona,
)
from ..deps import cached_image_response
from ..schemas import UserPersonaCreate, UserPersonaUpdate

router = APIRouter()


@router.get("/api/user-personas")
async def api_list_user_personas():
    return await get_user_personas()


@router.post("/api/user-personas")
async def api_create_user_persona(data: UserPersonaCreate):
    return await create_user_persona(data.model_dump())


@router.put("/api/user-personas/{persona_id}")
async def api_update_user_persona(persona_id: int, data: UserPersonaUpdate):
    update_data = data.model_dump(exclude_none=True)
    update_data.update(
        {field: getattr(data, field) for field in ("avatar_b64", "avatar_mime") if field in data.model_fields_set}
    )
    result = await update_user_persona(persona_id, update_data)
    if not result:
        raise HTTPException(status_code=404, detail="User persona not found")
    return result


@router.get("/api/user-personas/{persona_id}/avatar")
async def api_get_persona_avatar(persona_id: int, request: Request):
    result = await get_persona_avatar(persona_id)
    if not result:
        raise HTTPException(status_code=404, detail="No avatar found")
    image_bytes, mime_type = result
    return cached_image_response(image_bytes, mime_type, request)


@router.delete("/api/user-personas/{persona_id}")
async def api_delete_user_persona(persona_id: int):
    success = await delete_user_persona(persona_id)
    if not success:
        raise HTTPException(status_code=404, detail="User persona not found")
    return {"ok": True}
