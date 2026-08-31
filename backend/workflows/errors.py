"""Define the user-facing workflow failure type."""

from __future__ import annotations


class WorkflowUserFacingError(RuntimeError):
    """A sanitized hook failure that can be shown to the user."""
