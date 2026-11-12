"""The assistant's tools over the library's readers and writers, the proposal store and Apply, bound to a host per request."""

from __future__ import annotations

from collections.abc import Sequence

from ....core.settings import Settings
from ..contracts import AssistantTool, ViewContext
from .actions import build_action_tools
from .deletes import build_delete_tools
from .edits import build_edit_tools
from .fragments import build_fragment_tools
from .host import AssistantHost, ChatUnavailable, HostRefusal, MediaOutcome, MediaRun, NamedWorkflow, SchemaName
from .media import build_media_tools
from .proposals import Proposal, apply_proposals, open_proposals, pending_note, revise_proposal, undo_proposals
from .reads import build_read_tools
from .setters import build_setter_tools
from .targets import reply_target
from .toggles import build_toggle_tools, switch_summary

__all__ = [
    "AssistantHost",
    "ChatUnavailable",
    "HostRefusal",
    "MediaOutcome",
    "MediaRun",
    "NamedWorkflow",
    "Proposal",
    "SchemaName",
    "apply_proposals",
    "build_tools",
    "open_proposals",
    "pending_note",
    "reply_target",
    "revise_proposal",
    "switch_summary",
    "undo_proposals",
]


def build_tools(
    settings: Settings,
    view: ViewContext,
    host: AssistantHost,
    *,
    media: MediaRun | None = None,
    held: Sequence[Proposal] = (),
) -> list[AssistantTool]:
    """Every assistant tool, in their stable order, bound to the view the request was sent from and to *host*.

    *media* links the media tools to the run's Stop and progress; without it they are left out. *held* are the proposals
    earlier requests made that still wait for the user, which a further edit or delete of the same thing replaces.
    """
    return [
        *build_read_tools(settings, view, host),
        *build_toggle_tools(host),
        *build_setter_tools(view, host),
        *build_fragment_tools(host),
        *build_edit_tools(view, host, held),
        *build_delete_tools(view, host, held),
        *build_action_tools(view, host),
        *build_media_tools(view, media, host),
    ]
