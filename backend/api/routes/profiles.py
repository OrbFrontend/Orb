"""Profile routes: the sidebar header's profile picker."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...core import maintenance_lock
from ...database import get_active_profile_id, get_profile, get_settings, list_profiles
from ...features import backups, profiles
from ...workflows import live_settings_changed
from ..deps import profile_change_fence
from ..schemas import ProfileActive, ProfileCreate, ProfileRename
from .backups import finish_thread
from .endpoints import public_settings

router = APIRouter()


def _missing() -> HTTPException:
    return HTTPException(status_code=404, detail="Profile not found")


@router.get("/api/profiles")
async def api_list_profiles():
    rows = await list_profiles()
    return {"profiles": [{"id": row["id"], "name": row["name"]} for row in rows], "active_id": await get_active_profile_id()}


@router.post("/api/profiles")
async def api_create_profile(data: ProfileCreate):
    """A new profile with fresh defaults, or with ``copy_from`` a copy of that profile and everything it owns. Refused while a
    generation runs, as a switch is, since the picker switches to it next."""
    with profile_change_fence():
        row = await profiles.create_profile(data.name, data.copy_from)
    if row is None:
        raise _missing()
    return {"id": row["id"], "name": row["name"]}


@router.put("/api/profiles/active")
async def api_switch_profile(data: ProfileActive):
    """Switch profiles; answers the settings row the switch left live."""
    with profile_change_fence():
        before = await get_settings()
        if not await profiles.switch_profile(data.id):
            raise _missing()
        await live_settings_changed(before)
    return public_settings(await get_settings())


@router.patch("/api/profiles/{profile_id}")
async def api_rename_profile(profile_id: str, data: ProfileRename):
    row = await profiles.rename_profile(profile_id, data.name)
    if row is None:
        raise _missing()
    return {"id": row["id"], "name": row["name"]}


@router.delete("/api/profiles/{profile_id}")
async def api_delete_profile(profile_id: str):
    """Delete a profile, backing it up first since its fragments go with it; answers the settings row, which changes when the
    active profile was deleted."""
    with profile_change_fence():
        row = await get_profile(profile_id)
        if row is None:
            raise _missing()
        before = await get_settings()
        async with maintenance_lock():
            if len(await list_profiles()) > 1:  # deleting the last one is refused below, so there is nothing to back up
                if profile_id == await get_active_profile_id():
                    await profiles.capture_active()
                try:
                    await finish_thread(backups.snapshot_profile, profile_id, f"before deleting {row['name']}")
                except backups.PresetError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
            if not await profiles.delete_profile(profile_id):
                raise _missing()
        await live_settings_changed(before)
    return public_settings(await get_settings())
