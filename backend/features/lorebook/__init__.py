"""Lorebook feature slice — activation/rendering facade plus Dynamic Worlds.

**Activation and rendering** live in ``backend/inference/lorebook.py``. All of it
is prompt assembly, and the workflow toolkit's off-turn prefix builder
(``workflows/toolkit.py``) must render it byte-identically to the pipeline's —
``workflows`` sits below ``features``, so the logic sits at the ``inference``
layer both consumers may import. This facade keeps the established import path
for the layers above (``pipeline.context``, ``pipeline.state``,
``api.routes.conversations``).

**Dynamic Worlds** is local: :mod:`.proposals` is the pure catalog + validation
half (what the Agent may propose, checked against the live World) and
:mod:`.changesets` is the lifecycle half (accept, reject, undo, reset, and the
recorded hand delete), which persists through ``database/`` rather than owning
SQL of its own.

The per-turn threading bundle ``LorebookTurn`` is **not** here — it is a pipeline
concern and lives with the other per-turn contracts in ``pipeline/state.py``.
"""

from __future__ import annotations

from ...inference.lorebook import (
    AGENTIC_LOREBOOK_SCAN_DEPTH,
    DYNAMIC_SECTION_TITLE,
    LOREBOOK_SCAN_DEPTH,
    agentic_lorebook_active,
    build_lorebook_catalog,
    compute_agentic_lorebook_block,
    compute_constant_lorebook_block,
    compute_depth_lorebook_block,
    compute_lorebook_block,
    compute_lorebook_injection_block,
    is_dynamic,
    render_lorebook_block,
    select_active_entries,
    select_effective_entries,
    select_keyword_entries,
)
from .changesets import (
    accept_changeset,
    close_changeset,
    delete_entry,
    dynamic_enabled,
    invert_operations,
    reset_world_to_authored,
    stage_proposal,
    undo_changeset,
)
from .proposals import (
    ValidatedProposal,
    build_world_change_catalog,
    parse_proposal_call,
    split_by_world,
    validate_proposal,
)

__all__ = [
    # scan-depth constants
    "LOREBOOK_SCAN_DEPTH",
    "AGENTIC_LOREBOOK_SCAN_DEPTH",
    # gating
    "agentic_lorebook_active",
    "dynamic_enabled",
    # layer projection
    "DYNAMIC_SECTION_TITLE",
    "is_dynamic",
    "select_effective_entries",
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
    "compute_depth_lorebook_block",
    # Dynamic Worlds — proposal validation
    "ValidatedProposal",
    "build_world_change_catalog",
    "parse_proposal_call",
    "split_by_world",
    "validate_proposal",
    # Dynamic Worlds — changeset lifecycle
    "accept_changeset",
    "close_changeset",
    "delete_entry",
    "invert_operations",
    "reset_world_to_authored",
    "stage_proposal",
    "undo_changeset",
]
