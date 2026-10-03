"""Define the user-facing workflow failure types."""

from __future__ import annotations


class WorkflowUserFacingError(RuntimeError):
    """A sanitized hook failure that can be shown to the user."""


class WorkflowInputError(WorkflowUserFacingError):
    """The input the user supplied cannot be used, such as an unreadable upload."""


class WorkflowUnavailableError(WorkflowUserFacingError):
    """Something the workflow needs is not set up yet, such as a model not downloaded."""
