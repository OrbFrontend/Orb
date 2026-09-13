"""Prose Rewriter secondary-workflow declaration."""

from __future__ import annotations

from ..toolkit import Workflow

WORKFLOW_ID = "prose_rewriter"

prose_rewriter_workflow = Workflow(
    id=WORKFLOW_ID,
    display_name="Prose Rewriter",
)

__all__ = ["WORKFLOW_ID", "prose_rewriter_workflow"]
