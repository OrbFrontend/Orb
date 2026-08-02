"""What will actually execute one render.

Separate from `contracts.py` only because `adapters/base.py` needs it and
`render.py` needs `adapters/base.py` -- keeping the dataclass here is what stops
that from being a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RenderTarget:
    """The dynamic tier: what *this* resolved style/graph/model will honour.

    The static tier is `ImageBackendCapabilities` -- "can this backend ever?".
    This is "what will this one do?", which for ComfyUI is a per-graph question:
    a one-encoder prose graph has no negative slot no matter what the backend
    supports, and only a graph with a mapped `LoadImage` takes references.

    `supports_references` collapses into "`reference_slots` is non-empty", the
    same collapse the graph-level slot map already makes.

    `notes` carries user-facing disclosure for a replay that could not be
    honoured exactly. Substituting silently is the thing to avoid; refusing
    outright is not the alternative.
    """

    source: str
    # comfy: the imported graph's id. cloud: "" -- there is no graph.
    target_id: str
    # comfy: the checkpoint filename. cloud: the model id.
    model: str
    supports_negative_prompt: bool
    supports_seed: bool
    supports_dimensions: bool
    # comfy: None, the graph decides. cloud: the replay's recorded size, else config.
    width: int | None
    height: int | None
    reference_slots: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
