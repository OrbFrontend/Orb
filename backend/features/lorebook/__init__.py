"""Lorebook feature slice — facade over ``inference.lorebook``.

The activation/selection/rendering logic lives in ``backend/inference/lorebook.py``:
the constant-entry prefix section (:func:`compute_constant_lorebook_block`) is
part of prompt assembly, and the workflow toolkit's off-turn prefix builder
(``workflows/toolkit.py``) must render it byte-identically to the pipeline's —
``workflows`` sits below ``features``, so the logic sits at the ``inference``
layer both consumers may import. This facade keeps the established import path
for the layers above (``pipeline.context``, ``pipeline.state``,
``api.routes.conversations``).

The per-turn threading bundle ``LorebookTurn`` is **not** here — it is a pipeline
concern and lives with the other per-turn contracts in ``pipeline/state.py``.
"""

from __future__ import annotations

from ...inference.lorebook import (
    AGENTIC_LOREBOOK_SCAN_DEPTH,
    LOREBOOK_SCAN_DEPTH,
    agentic_lorebook_active,
    build_lorebook_catalog,
    compute_agentic_lorebook_block,
    compute_constant_lorebook_block,
    compute_lorebook_block,
    compute_lorebook_injection_block,
    render_lorebook_block,
    select_active_entries,
    select_keyword_entries,
)

__all__ = [
    # scan-depth constants
    "LOREBOOK_SCAN_DEPTH",
    "AGENTIC_LOREBOOK_SCAN_DEPTH",
    # gating
    "agentic_lorebook_active",
    # director-facing catalog
    "build_lorebook_catalog",
    # selection + rendering
    "select_active_entries",
    "select_keyword_entries",
    "render_lorebook_block",
    # block builders
    "compute_lorebook_block",
    "compute_lorebook_injection_block",
    "compute_agentic_lorebook_block",
    "compute_constant_lorebook_block",
]
