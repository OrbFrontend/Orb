"""The panel assistant's API-side composition: its tools over the existing readers and writers, and Apply.

The model loop itself is ``features.assistant``; this package supplies the callables it runs, bound to one request.
"""

from __future__ import annotations

from ...core.settings import Settings
from ...features.assistant import AssistantTool, ViewContext
from .actions import build_action_tools
from .edits import build_edit_tools
from .media import MediaRun, build_media_tools
from .proposals import apply_proposals
from .reads import build_read_tools
from .toggles import build_toggle_tools

__all__ = ["MediaRun", "apply_proposals", "build_tools"]


def build_tools(settings: Settings, view: ViewContext, *, media: MediaRun | None = None) -> list[AssistantTool]:
    """Every assistant tool, in their stable order, bound to the view the request was sent from.

    *media* links the media tools to the run's Stop and progress; without it they are left out.
    """
    return [
        *build_read_tools(settings, view),
        *build_toggle_tools(),
        *build_edit_tools(view),
        *build_action_tools(view),
        *build_media_tools(view, media),
    ]
