"""The panel assistant's API-side composition: its tools over the existing readers and writers, and Apply.

The model loop itself is ``features.assistant``; this package supplies the callables it runs, bound to one request.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...core.settings import Settings
from ...features.assistant import AssistantTool, ViewContext
from .actions import build_action_tools
from .deletes import build_delete_tools
from .edits import build_edit_tools
from .follow_up import keep_follow_up, take_follow_up
from .fragments import build_fragment_tools
from .media import MediaRun, build_media_tools
from .proposals import Proposal, apply_proposals, open_proposals, pending_note, revise_proposal, undo_proposals
from .reads import build_read_tools
from .setters import build_setter_tools
from .toggles import build_toggle_tools

__all__ = [
    "MediaRun",
    "Proposal",
    "apply_proposals",
    "build_tools",
    "keep_follow_up",
    "open_proposals",
    "pending_note",
    "revise_proposal",
    "take_follow_up",
    "undo_proposals",
]


def build_tools(
    settings: Settings, view: ViewContext, *, media: MediaRun | None = None, held: Sequence[Proposal] = ()
) -> list[AssistantTool]:
    """Every assistant tool, in their stable order, bound to the view the request was sent from.

    *media* links the media tools to the run's Stop and progress; without it they are left out. *held* are the proposals
    earlier requests made that still wait for the user, which a further edit or delete of the same thing replaces.
    """
    return [
        *build_read_tools(settings, view),
        *build_toggle_tools(),
        *build_setter_tools(view),
        *build_fragment_tools(),
        *build_edit_tools(view, held),
        *build_delete_tools(view, held),
        *build_action_tools(view),
        *build_media_tools(view, media),
    ]
