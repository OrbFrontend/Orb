"""Mood-fragment and interactive-fragment CRUD routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from ...database import (
    create_interactive_fragment,
    create_mood_fragment,
    delete_interactive_fragment,
    delete_mood_fragment,
    get_interactive_fragment,
    get_interactive_fragments,
    get_mood_fragment,
    get_mood_fragments,
    update_interactive_fragment,
    update_mood_fragment,
)
from ...workflows import FragmentTypeError, RegistrySnapshot, current_snapshot
from ..schemas import (
    InteractiveFragmentCreate,
    InteractiveFragmentUpdate,
    MoodFragmentCreate,
    MoodFragmentUpdate,
)

router = APIRouter()

_DEDICATED_TYPES = (
    {
        "id": "feedback",
        "label": "Feedback (note to you)",
        "description": "An out-of-character note shown after the reply.",
        "kind": "dedicated",
    },
    {
        "id": "direction_note",
        "label": "Direction note (persists)",
        "description": "A lasting branch direction note recorded by the Director.",
        "kind": "dedicated",
    },
)


# Mood Fragments ──


@router.get("/api/fragments")
async def api_list_mood_fragments():
    return await get_mood_fragments()


@router.post("/api/fragments")
async def api_create_mood_fragment(data: MoodFragmentCreate):
    existing = await get_mood_fragment(data.id)
    if existing:
        raise HTTPException(status_code=400, detail="Mood fragment with this ID already exists")
    return await create_mood_fragment(data.model_dump())


@router.put("/api/fragments/{fid}")
async def api_update_mood_fragment(fid: str, data: MoodFragmentUpdate):
    result = await update_mood_fragment(fid, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(status_code=404, detail="Mood fragment not found")
    return result


@router.delete("/api/fragments/{fid}")
async def api_delete_mood_fragment(fid: str):
    if not await delete_mood_fragment(fid):
        raise HTTPException(status_code=404, detail="Mood fragment not found or is built-in")
    return {"ok": True}


# Interactive Fragments ──


@router.get("/api/interactive-fragments")
async def api_list_interactive_fragments():
    snapshot = current_snapshot()
    return [_fragment_view(row, snapshot=snapshot) for row in await get_interactive_fragments()]


@router.get("/api/interactive-fragment-types")
async def api_list_interactive_fragment_types():
    snapshot = current_snapshot()
    entries = []
    for definition in snapshot.fragment_types.values():
        entries.append(
            {
                "id": definition.type_id,
                "local_id": definition.local_id,
                "label": definition.label,
                "description": definition.description,
                "kind": "contributed" if definition.contributed else "core",
                "storage": definition.storage,
                "owner_id": definition.owner_id,
                "content_digest": definition.content_digest,
                "config_schema": dict(definition.config_schema),
                "config_view": dict(definition.config_view) if definition.config_view is not None else None,
                "value_view": dict(definition.value_view) if definition.value_view is not None else None,
            }
        )
    entries.extend(_DEDICATED_TYPES)
    return {"runtime_generation": snapshot.generation, "types": entries}


@router.post("/api/interactive-fragments")
async def api_create_interactive_fragment(data: InteractiveFragmentCreate):
    existing = await get_interactive_fragment(data.id)
    if existing:
        raise HTTPException(status_code=400, detail="Interactive fragment with this ID already exists")
    payload = data.model_dump()
    _validate_fragment_type(payload["field_type"], payload.get("type_config"))
    result = await create_interactive_fragment(payload)
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create interactive fragment")
    return _fragment_view(result)


@router.put("/api/interactive-fragments/{fid}")
async def api_update_interactive_fragment(fid: str, data: InteractiveFragmentUpdate):
    existing = await get_interactive_fragment(fid)
    if not existing:
        raise HTTPException(status_code=404, detail="Interactive fragment not found")
    payload = data.model_dump(exclude_none=True)
    merged = {**existing, **payload}
    _validate_fragment_type(str(merged.get("field_type") or "string"), merged.get("type_config"))
    result = await update_interactive_fragment(fid, payload)
    if not result:
        raise HTTPException(status_code=404, detail="Interactive fragment not found")
    return _fragment_view(result)


@router.delete("/api/interactive-fragments/{fid}")
async def api_delete_interactive_fragment(fid: str):
    if not await delete_interactive_fragment(fid):
        raise HTTPException(status_code=404, detail="Interactive fragment not found")
    return {"ok": True}


def _decode_config(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw or "{}")
        except ValueError:
            return None
        return dict(decoded) if isinstance(decoded, dict) else None
    return {} if raw is None else None


def _validate_fragment_type(field_type: str, raw_config: Any) -> None:
    config = _decode_config(raw_config)
    if config is None:
        raise HTTPException(status_code=422, detail="type_config must be a JSON object")
    if field_type in ("feedback", "direction_note"):
        if config:
            raise HTTPException(status_code=422, detail=f"{field_type} fragments do not accept type_config")
        return
    definition = current_snapshot().fragment_type(field_type)
    if definition is None:
        status = 409 if ":" in field_type else 422
        raise HTTPException(status_code=status, detail=f"fragment type {field_type!r} is unavailable")
    try:
        definition.instantiate(config)
    except (FragmentTypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _fragment_view(row: Any, *, snapshot: RegistrySnapshot | None = None) -> dict[str, Any]:
    view = dict(row)
    config = _decode_config(view.get("type_config"))
    view["type_config"] = config if config is not None else {}
    field_type = str(view.get("field_type") or "string")
    registry = snapshot or current_snapshot()
    if config is None:
        view["type_available"] = False
        view["type_diagnostic"] = "Fragment type configuration is not a JSON object"
        return view
    if field_type in ("feedback", "direction_note"):
        view["type_available"] = not config
        if config:
            view["type_diagnostic"] = f"{field_type} fragments do not accept type configuration"
        return view
    definition = registry.fragment_type(field_type)
    if definition is None:
        view["type_available"] = False
        owner = field_type.split(":", 1)[0] if ":" in field_type else field_type
        view["type_diagnostic"] = f"Fragment type provider {owner!r} is unavailable"
        return view
    try:
        definition.instantiate(config)
    except (FragmentTypeError, ValueError) as exc:
        view["type_available"] = False
        view["type_diagnostic"] = str(exc)
        return view
    view["type_available"] = True
    return view
