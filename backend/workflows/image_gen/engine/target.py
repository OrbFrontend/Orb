"""What will actually execute one render.

Separate from `contracts.py` only because `adapters/base.py` needs it and
`render.py` needs `adapters/base.py`; keeping it here stops that being a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RenderTarget:
    """The dynamic tier: what *this* resolved style/graph/model will honour.

    `ImageBackendCapabilities` is the static tier ("can this backend ever?"); this
    is "what will this one do?", a per-graph question for ComfyUI. There is no
    `supports_references` -- it collapses into "`reference_slots` is non-empty".

    `notes` carries user-facing disclosure for a replay that could not be honoured
    exactly: substituting silently is the thing to avoid, and refusing outright is
    not the alternative.
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
