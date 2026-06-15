"""Miscellaneous routes: frontend shell, themes, factory reset."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...database import reset_to_defaults
from ..deps import FRONTEND_DIR
from ..schemas import ResetConfirm

router = APIRouter()


@router.get("/api/themes")
async def api_get_themes():
    themes_dir = os.path.join(FRONTEND_DIR, "themes")
    names = sorted(f[:-4] for f in os.listdir(themes_dir) if f.endswith(".css"))
    return {"themes": names}


@router.post("/api/reset")
async def api_reset(data: ResetConfirm):
    """Reset mood_fragments, interactive_fragments, phrase_bank, and settings to defaults."""
    if not data.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    await reset_to_defaults()
    return {"ok": True}


@router.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
