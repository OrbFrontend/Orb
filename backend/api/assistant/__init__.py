"""The panel assistant's API-side binding: the feature's tools and Apply bound to the app's host.

The tools, the proposal store and the model loop are ``features.assistant``; this package supplies the host they run against.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ...core.settings import Settings
from ...features.assistant import AssistantTool, ViewContext, keep_follow_up, take_follow_up, tools
from ...features.assistant.tools import MediaRun, Proposal, open_proposals, pending_note
from .host import HOST

__all__ = [
    "HOST",
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
    return tools.build_tools(settings, view, HOST, media=media, held=held)


async def apply_proposals(proposal_ids: Sequence[str]) -> list[dict[str, Any]]:
    return await tools.apply_proposals(proposal_ids, HOST)


async def undo_proposals(proposal_ids: Sequence[str]) -> list[dict[str, Any]]:
    return await tools.undo_proposals(proposal_ids, HOST)


async def revise_proposal(proposal_id: str, text: str) -> dict[str, Any]:
    return await tools.revise_proposal(proposal_id, text, HOST)
