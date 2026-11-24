"""Prompt layout and prompt block routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException

from ...core.prompt_layout import CUSTOM_KIND
from ...core.settings import Settings
from ...database import (
    PromptLayoutError,
    create_prompt_block,
    create_prompt_layout,
    delete_prompt_block,
    delete_prompt_layout,
    get_active_prompt_layout_id,
    get_prompt_blocks,
    get_prompt_layout,
    get_settings,
    list_prompt_layouts,
    rename_prompt_layout,
    reorder_prompt_blocks,
    set_active_prompt_layout,
    update_prompt_block,
)
from ...pipeline.passes.editor.length_guard import writer_nudge
from ...prompting.layout import TOOL_NOTICE, marker_text
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


# What an engine marker's dynamic part stands for, shown in its fixed framing.
_MARKER_PLACEHOLDERS: dict[str, str] = {
    "cast": "[The group's cast and each member's sheet]",
    "constant_lore": "[Constant lore entries]",
    "workflow_blocks": "[Text that enabled workflows add]",
    "selected_lore": "[Lore entries chosen for this turn]",
    "scene_guidance": "[The Director's guidance for this turn]",
    "cue": "[The Director's cue for this speaker]",
    "user_message": "[The user's message]",
    "depth_lore": "[Depth lore entries]",
}


def _builtin_text(kind: str, settings: Settings) -> str:
    """The text the engine sends for a marker, with its dynamic part as a placeholder; empty for editable blocks."""
    if kind == "length_nudge":
        nudge = writer_nudge(
            {
                "enforce": True,
                "max_words": int(settings.get("length_guard_max_words", 240)),
                "max_paragraphs": int(settings.get("length_guard_max_paragraphs", 4)),
            }
        )
        return f"[When the request carries tool schemas]\n{TOOL_NOTICE}{nudge.strip()}"
    placeholder = _MARKER_PLACEHOLDERS.get(kind)
    return marker_text(kind, placeholder) if placeholder else ""


def _block_view(row: Mapping[str, Any], settings: Settings) -> dict[str, Any]:
    """A block row plus the built-in text a marker sends."""
    return {**row, "builtin_text": "" if row["kind"] == CUSTOM_KIND else _builtin_text(row["kind"], settings)}


async def _blocks(layout_id: str) -> list[dict[str, Any]]:
    settings = await get_settings()
    return [_block_view(row, settings) for row in await get_prompt_blocks(layout_id)]


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
        return _block_view(await create_prompt_block(layout_id, data.model_dump()), await get_settings())
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
    return _block_view(row, await get_settings())


@router.delete("/api/prompt-blocks/{block_id}")
async def api_delete_prompt_block(block_id: str):
    try:
        deleted = await delete_prompt_block(block_id)
    except PromptLayoutError as error:
        raise _refused(error) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="Prompt block not found")
    return {"ok": True}
