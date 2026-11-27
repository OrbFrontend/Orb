"""Switch between profiles.

``database.queries.profiles`` holds what a profile saves and how entering one writes it live, each switch in one transaction.
This module serializes the changes that touch the active profile, so a capture never lands between another change's steps, and
decides which profile a delete or a Backups apply leaves active.
"""

from __future__ import annotations

import asyncio

from ...database import (
    ProfileEditError,
    enter_profile,
    get_active_profile_id,
    list_profiles,
    save_live_state,
    seed_missing_layouts,
)
from ...database import create_profile as insert_profile
from ...database import delete_profile as remove_profile
from ...database.models import ProfileRow

# Serializes switches, copies, and the captures and deletes that touch the active profile.
_lock = asyncio.Lock()


async def switch_profile(profile_id: str) -> bool:
    """Leave the active profile, saving the live state into it, and enter *profile_id*. False when it does not exist."""
    async with _lock:
        return await enter_profile(profile_id, save_leaving=True)


async def create_profile(name: str, copy_from: str | None = None) -> ProfileRow | None:
    """A new profile, or a copy of *copy_from*; see ``database.create_profile``. None when *copy_from* is missing."""
    async with _lock:
        return await insert_profile(name, copy_from)


async def delete_profile(profile_id: str) -> bool:
    """Delete the profile and its library; deleting the active one enters the first profile left. False when it does not
    exist."""
    async with _lock:
        profiles = await list_profiles()
        remaining = [row for row in profiles if row["id"] != profile_id]
        if len(remaining) == len(profiles):
            return False
        if not remaining:
            raise ProfileEditError("The last profile cannot be deleted.")
        if profile_id == await get_active_profile_id():
            await enter_profile(remaining[0]["id"], save_leaving=False)
        await remove_profile(profile_id)
    return True


async def capture_active() -> str:
    """Save the live state into the active profile, so what it holds is what is live; returns its id."""
    async with _lock:
        active = await get_active_profile_id()
        await save_live_state(active)
    return active


async def reenter(profile_id: str) -> None:
    """Enter *profile_id* again, or the first profile when it is gone, after something rewrote the live rows or the profile
    under it; a profile that came without a layout gets the built-in one."""
    async with _lock:
        await seed_missing_layouts()
        if not await enter_profile(profile_id, save_leaving=False):
            await enter_profile(await get_active_profile_id(), save_leaving=False)
