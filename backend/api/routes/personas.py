"""User-persona CRUD routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...database import (
    create_user_persona,
    delete_user_persona,
    get_user_personas,
    update_user_persona,
)
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
    result = await update_user_persona(persona_id, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(status_code=404, detail="User persona not found")
    return result


@router.delete("/api/user-personas/{persona_id}")
async def api_delete_user_persona(persona_id: int):
    success = await delete_user_persona(persona_id)
    if not success:
        raise HTTPException(status_code=404, detail="User persona not found")
    return {"ok": True}
