"""Prompt block routes: the active profile's prompt layout. ``profiles.py`` serves the profiles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException

from ...core.macros import has_clock_macros
from ...core.prompt_layout import CUSTOM_KIND, EDITABLE_MARKERS
from ...database import (
    create_prompt_block,
    delete_prompt_block,
    get_prompt_blocks,
    reorder_prompt_blocks,
    update_prompt_block,
)
from ..schemas import PromptBlockCreate, PromptBlockOrder, PromptBlockUpdate

router = APIRouter()
_T = TypeVar("_T")


def _found(value: _T | None) -> _T:
    """*value*, or a 404 when the query found nothing. Refused edits raise ``PromptLayoutError``, answered 409 app-wide."""
    if not value:
        raise HTTPException(status_code=404, detail="Prompt block not found")
    return value


def _block_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A block row plus whether its text is editable, and whether it reads the clock above the chat history, where every
    change re-reads the chat."""
    return {
        **row,
        "editable": row["kind"] == CUSTOM_KIND or row["kind"] in EDITABLE_MARKERS,
        "clock": row["region"] == "prefix" and has_clock_macros(row["content"]),
    }


@router.get("/api/prompt-blocks")
async def api_list_prompt_blocks():
    return [_block_view(row) for row in await get_prompt_blocks()]


@router.post("/api/prompt-blocks")
async def api_create_prompt_block(data: PromptBlockCreate):
    return _block_view(await create_prompt_block(data.model_dump()))


@router.put("/api/prompt-blocks/order")
async def api_reorder_prompt_blocks(data: PromptBlockOrder):
    return [_block_view(row) for row in await reorder_prompt_blocks(data.prefix, data.writer)]


@router.patch("/api/prompt-blocks/{block_id}")
async def api_update_prompt_block(block_id: str, data: PromptBlockUpdate):
    return _block_view(_found(await update_prompt_block(block_id, data.model_dump(exclude_none=True))))


@router.delete("/api/prompt-blocks/{block_id}")
async def api_delete_prompt_block(block_id: str):
    _found(await delete_prompt_block(block_id))
    return {"ok": True}
