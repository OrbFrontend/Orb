"""Lorebook activation, rendering, and Dynamic Worlds helpers."""

from __future__ import annotations

from ...prompting.lorebook import (
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
from .interchange import lorebook_to_book, normalise_lorebook_entry, project_lorebook_view
from .proposals import ValidatedProposal, build_world_change_catalog, parse_proposal_call, split_by_world, validate_proposal
