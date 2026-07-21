"""Source/style resolution, replay targeting, and adapter routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..config import resolve_style
from .adapters import external_comfy
from .comfy_client import ProgressCallback
from .contracts import ImageRequest, ImageResult
from .graph import has_graph


@dataclass(frozen=True)
class RenderTarget:
    """What will actually execute: which graph, on which checkpoint, and why.

    `notes` carries user-facing disclosure for a replay that could not be
    honoured exactly. Substituting silently is the thing to avoid; refusing
    outright is not the alternative.
    """

    graph_id: str
    checkpoint: str
    notes: tuple[str, ...] = ()


def resolve_render_target(
    config: Mapping[str, Any],
    style_id: str,
    replay: Mapping[str, Any] | None = None,
) -> RenderTarget:
    """Pick the graph/checkpoint for a fresh render, or for replaying a stored one.

    A fresh render follows the style (its pins, else the global selection). A
    replay follows what the stored image recorded, because reroll and rehydrate
    promise the *same* image parameters with a different (or identical) seed --
    resolving through the style instead would silently re-render an old
    attachment on whatever checkpoint the style points at today.
    """
    style = resolve_style(config, style_id)
    if not replay:
        return RenderTarget(style["workflow"], style["checkpoint"])

    notes: list[str] = []
    stored_graph = replay.get("workflow_id")
    graph_id = stored_graph if isinstance(stored_graph, str) and stored_graph else ""
    if graph_id and not has_graph(config, graph_id):
        notes.append(f"the workflow this image used ({graph_id}) is gone; rendered with {style['workflow']!r} instead")
        graph_id = ""
    stored_checkpoint = replay.get("backend_model")
    # An empty stored checkpoint means the original ran a user graph carrying its
    # own loaders, where the value is ignored anyway -- fall through rather than
    # inventing a pin the original never had.
    checkpoint = stored_checkpoint if isinstance(stored_checkpoint, str) and stored_checkpoint else style["checkpoint"]
    return RenderTarget(graph_id or style["workflow"], checkpoint, tuple(notes))


async def resolve_and_generate(
    config: Mapping[str, Any],
    request: ImageRequest,
    *,
    replay: Mapping[str, Any] | None = None,
    progress: ProgressCallback | None = None,
) -> ImageResult:
    target = resolve_render_target(config, request.style_id, replay)
    return await external_comfy.generate(
        config,
        request,
        checkpoint=target.checkpoint,
        graph_id=target.graph_id,
        notes=target.notes,
        progress=progress,
    )
