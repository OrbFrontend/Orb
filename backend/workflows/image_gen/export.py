"""Export a stored render as a full-quality PNG."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import PurePosixPath

from ..toolkit import ExportedFile, get_workflow_config
from .config import WORKFLOW_ID, normalize_config
from .engine import comfy_adapter
from .engine.display_encode import lossless_png

_UNFETCHED = "The original could not be fetched from ComfyUI, so this PNG was converted from Orb's stored copy."
_STORED_ONLY = "Orb keeps only a compressed copy of this image, so this PNG was converted from it."


def _filename(ctx) -> str:
    stem = PurePosixPath(str(ctx.attachment.get("filename") or "")).stem or "generated-image"
    return f"{stem}-{ctx.attachment_id}.png"


async def export(ctx) -> ExportedFile | None:
    """The render as a PNG, from the best copy that still exists.

    Orb stores renders re-encoded for display and never keeps a second, larger copy.
    The full-quality file lives where it was made: ComfyUI keeps what it saved, so
    that is fetched on demand. A cloud render leaves no such file, and neither does
    one ComfyUI has since dropped; those fall back to the stored copy, and the note
    says so.
    """
    output = (ctx.consumption_metadata or {}).get("comfy_output")
    note = _STORED_ONLY
    if isinstance(output, Mapping):
        config = normalize_config(await get_workflow_config(WORKFLOW_ID))
        original = await comfy_adapter(config).fetch_output(output)
        if original is not None:
            return ExportedFile(data=await asyncio.to_thread(lossless_png, original), mime="image/png", filename=_filename(ctx))
        note = _UNFETCHED
    stored = await ctx.stored_bytes()
    if stored is None:
        return None
    png = await asyncio.to_thread(lossless_png, stored)
    # A stored PNG is the source's own bytes: the display copy keeps them whenever
    # WebP would not have been smaller, so nothing was lost and there is nothing to say.
    return ExportedFile(data=png, mime="image/png", filename=_filename(ctx), note="" if png is stored else note)
