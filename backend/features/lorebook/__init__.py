"""Lorebook activation, rendering, and Dynamic Worlds helpers."""

from __future__ import annotations

from ...prompting.lorebook import (
    AGENTIC_LOREBOOK_SCAN_DEPTH,
    DYNAMIC_SECTION_TITLE,
    LOREBOOK_SCAN_DEPTH,
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
from .enablement import agentic_lorebook_active
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
