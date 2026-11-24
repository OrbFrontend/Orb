"""Prompt layout and prompt block routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException

from ...core.prompt_layout import CUSTOM_KIND, EDITABLE_MARKERS
from ...database import (
    create_prompt_block,
    create_prompt_layout,
    delete_prompt_block,
    delete_prompt_layout,
    get_active_prompt_layout_id,
    get_prompt_blocks,
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
_T = TypeVar("_T")


def _found(value: _T | None, what: str = "Prompt layout") -> _T:
    """*value*, or a 404 when the query found nothing. Refused edits raise ``PromptLayoutError``, answered 409 app-wide."""
    if not value:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return value


# What a marker whose text the user does not edit stands for.
_MARKER_PLACEHOLDERS: dict[str, str] = {
    "cast": "[The group's cast and each member's sheet]",
}


def _block_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A block row plus whether its text is editable and, for a marker that is not, the text it sends."""
    editable = row["kind"] == CUSTOM_KIND or row["kind"] in EDITABLE_MARKERS
    return {**row, "editable": editable, "builtin_text": "" if editable else _MARKER_PLACEHOLDERS.get(row["kind"], "")}


@router.get("/api/prompt-layouts")
async def api_list_prompt_layouts():
    return {"layouts": await list_prompt_layouts(), "active_id": await get_active_prompt_layout_id()}


@router.post("/api/prompt-layouts")
async def api_create_prompt_layout(data: PromptLayoutCreate):
    return _found(await create_prompt_layout(data.name, data.copy_from))


@router.put("/api/prompt-layouts/active")
async def api_set_active_prompt_layout(data: PromptLayoutActive):
    _found(await set_active_prompt_layout(data.id))
    return {"active_id": data.id}


@router.patch("/api/prompt-layouts/{layout_id}")
async def api_rename_prompt_layout(layout_id: str, data: PromptLayoutRename):
    return _found(await rename_prompt_layout(layout_id, data.name))


@router.delete("/api/prompt-layouts/{layout_id}")
async def api_delete_prompt_layout(layout_id: str):
    _found(await delete_prompt_layout(layout_id))
    return {"ok": True}


@router.get("/api/prompt-layouts/{layout_id}/blocks")
async def api_list_prompt_blocks(layout_id: str):
    # Every layout holds its markers, so no blocks means no layout.
    return [_block_view(row) for row in _found(await get_prompt_blocks(layout_id))]


@router.post("/api/prompt-layouts/{layout_id}/blocks")
async def api_create_prompt_block(layout_id: str, data: PromptBlockCreate):
    return _block_view(_found(await create_prompt_block(layout_id, data.model_dump())))


@router.put("/api/prompt-layouts/{layout_id}/order")
async def api_reorder_prompt_blocks(layout_id: str, data: PromptBlockOrder):
    return [_block_view(row) for row in _found(await reorder_prompt_blocks(layout_id, data.prefix, data.writer))]


@router.patch("/api/prompt-blocks/{block_id}")
async def api_update_prompt_block(block_id: str, data: PromptBlockUpdate):
    return _block_view(_found(await update_prompt_block(block_id, data.model_dump(exclude_none=True)), "Prompt block"))


@router.delete("/api/prompt-blocks/{block_id}")
async def api_delete_prompt_block(block_id: str):
    _found(await delete_prompt_block(block_id), "Prompt block")
    return {"ok": True}
