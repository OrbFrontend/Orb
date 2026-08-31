"""Workflow binding for deterministic format normalization."""

from __future__ import annotations

from ..registry import Workflow

format_consistency_workflow = Workflow(
    id="format_consistency",
    display_name="Format Consistency",
)
