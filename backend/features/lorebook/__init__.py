"""Lorebook feature slice — pure world-info activation, selection, and rendering.

A ``features/`` slice: pure domain logic that depends only **downward** on
``core`` (for ``Macros``) — no ``inference``, ``database``, or peer slice. Its
selection/rendering is consumed by the director stage
(``pipeline.passes.director``, via the ``LorebookTurn`` bundle) and the
context-size route (``api.routes.conversations``) — both layers *above* the
slice, so the one-way rule holds. The constant-entry prefix section
(:func:`compute_constant_lorebook_block`) is consumed by the prefix assembly in
``pipeline.context`` and the context-size route.

The per-turn threading bundle ``LorebookTurn`` is **not** here — it is a pipeline
concern and lives with the other per-turn contracts in ``pipeline/state.py``.

The facade re-exports the activation functions and the scan-depth constants.
"""

from __future__ import annotations

from .activation import (
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
