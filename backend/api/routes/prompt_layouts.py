"""Prompt layout and prompt block routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException

from ...core.prompt_layout import CUSTOM_KIND, EDITABLE_MARKERS
from ...database import (
    PromptLayoutError,
    create_prompt_block,
    create_prompt_layout,
    delete_prompt_block,
    delete_prompt_layout,
    get_active_prompt_layout_id,
    get_prompt_blocks,
    get_prompt_layout,
    list_prompt_layouts,
    rename_prompt_layout,
    reorder_prompt_blocks,
    set_active_prompt_layout,
    update_prompt_block,
)
from ..schemas import (
    PromptBlockCreate,
    PromptBlockOrder,
    PromptBlockUpdate,
    PromptLayoutActive,
    PromptLayoutCreate,
    PromptLayoutRename,
)

router = APIRouter()


def _refused(error: PromptLayoutError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(error))


# What a marker whose text the user does not edit stands for.
_MARKER_PLACEHOLDERS: dict[str, str] = {
    "cast": "[The group's cast and each member's sheet]",
}


def _block_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A block row plus whether its text is editable and, for a marker that is not, the text it sends."""
    editable = row["kind"] == CUSTOM_KIND or row["kind"] in EDITABLE_MARKERS
    return {**row, "editable": editable, "builtin_text": "" if editable else _MARKER_PLACEHOLDERS.get(row["kind"], "")}


async def _blocks(layout_id: str) -> list[dict[str, Any]]:
    return [_block_view(row) for row in await get_prompt_blocks(layout_id)]


@router.get("/api/prompt-layouts")
async def api_list_prompt_layouts():
    return {"layouts": await list_prompt_layouts(), "active_id": await get_active_prompt_layout_id()}


@router.post("/api/prompt-layouts")
async def api_create_prompt_layout(data: PromptLayoutCreate):
    try:
        return await create_prompt_layout(data.name, data.copy_from)
    except PromptLayoutError as error:
        raise _refused(error) from None


@router.put("/api/prompt-layouts/active")
async def api_set_active_prompt_layout(data: PromptLayoutActive):
    if not await set_active_prompt_layout(data.id):
        raise HTTPException(status_code=404, detail="Prompt layout not found")
    return {"active_id": data.id}


@router.patch("/api/prompt-layouts/{layout_id}")
async def api_rename_prompt_layout(layout_id: str, data: PromptLayoutRename):
    try:
        layout = await rename_prompt_layout(layout_id, data.name)
    except PromptLayoutError as error:
        raise _refused(error) from None
    if layout is None:
        raise HTTPException(status_code=404, detail="Prompt layout not found")
    return layout


@router.delete("/api/prompt-layouts/{layout_id}")
async def api_delete_prompt_layout(layout_id: str):
    try:
        deleted = await delete_prompt_layout(layout_id)
    except PromptLayoutError as error:
        raise _refused(error) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="Prompt layout not found")
    return {"ok": True}


@router.get("/api/prompt-layouts/{layout_id}/blocks")
async def api_list_prompt_blocks(layout_id: str):
    if await get_prompt_layout(layout_id) is None:
        raise HTTPException(status_code=404, detail="Prompt layout not found")
    return await _blocks(layout_id)


@router.post("/api/prompt-layouts/{layout_id}/blocks")
async def api_create_prompt_block(layout_id: str, data: PromptBlockCreate):
    try:
        return _block_view(await create_prompt_block(layout_id, data.model_dump()))
    except LookupError:
        raise HTTPException(status_code=404, detail="Prompt layout not found") from None
    except PromptLayoutError as error:
        raise _refused(error) from None


@router.put("/api/prompt-layouts/{layout_id}/order")
async def api_reorder_prompt_blocks(layout_id: str, data: PromptBlockOrder):
    try:
        await reorder_prompt_blocks(layout_id, data.prefix, data.writer)
    except LookupError:
        raise HTTPException(status_code=404, detail="Prompt layout not found") from None
    except PromptLayoutError as error:
        raise _refused(error) from None
    return await _blocks(layout_id)


@router.patch("/api/prompt-blocks/{block_id}")
async def api_update_prompt_block(block_id: str, data: PromptBlockUpdate):
    try:
        row = await update_prompt_block(block_id, data.model_dump(exclude_none=True))
    except PromptLayoutError as error:
        raise _refused(error) from None
    if row is None:
        raise HTTPException(status_code=404, detail="Prompt block not found")
    return _block_view(row)


@router.delete("/api/prompt-blocks/{block_id}")
async def api_delete_prompt_block(block_id: str):
    try:
        deleted = await delete_prompt_block(block_id)
    except PromptLayoutError as error:
        raise _refused(error) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="Prompt block not found")
    return {"ok": True}
