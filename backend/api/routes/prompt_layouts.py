"""Prompt block routes: a profile's prompt layout. ``{layout_id}`` is the profile's id; ``profiles.py`` serves the profiles."""

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


def _found(value: _T | None, what: str = "Profile") -> _T:
    """*value*, or a 404 when the query found nothing. Refused edits raise ``PromptLayoutError``, answered 409 app-wide."""
    if not value:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return value


def _block_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A block row plus whether its text is editable, and whether it reads the clock above the chat history, where every
    change re-reads the chat."""
    return {
        **row,
        "editable": row["kind"] == CUSTOM_KIND or row["kind"] in EDITABLE_MARKERS,
        "clock": row["region"] == "prefix" and has_clock_macros(row["content"]),
    }


@router.get("/api/prompt-layouts/{layout_id}/blocks")
async def api_list_prompt_blocks(layout_id: str):
    # Every profile's layout holds its markers, so no blocks means no profile.
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
