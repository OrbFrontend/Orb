"""The panel assistant's API-side binding: the feature's tools and Apply bound to the app's host.

The tools, the proposal store and the model loop are ``features.assistant``; this package supplies the host they run against.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from ...core.settings import Settings
from ...features.assistant import (
    AssistantTool,
    ViewContext,
    chat_moved,
    flush_transcripts,
    forget_transcript,
    held_transcript,
    hold_transcript,
    rewind_note,
    rewound,
    tools,
)
from ...features.assistant.tools import MediaRun, Proposal, open_proposals, pending_note, withheld_labels
from .host import HOST

__all__ = [
    "HOST",
    "MediaRun",
    "Proposal",
    "apply_proposals",
    "build_tools",
    "chat_moved",
    "flush_transcripts",
    "forget_transcript",
    "held_transcript",
    "hold_transcript",
    "open_proposals",
    "pending_note",
    "revise_proposal",
    "rewind_note",
    "rewound",
    "undo_proposals",
    "withheld_labels",
]


def build_tools(
    settings: Settings,
    view: ViewContext,
    *,
    media: MediaRun | None = None,
    held: Sequence[Proposal] = (),
    categories: Collection[str] | None = None,
) -> list[AssistantTool]:
    return tools.build_tools(settings, view, HOST, media=media, held=held, categories=categories)


async def apply_proposals(proposal_ids: Sequence[str]) -> list[dict[str, Any]]:
    return await tools.apply_proposals(proposal_ids, HOST)


async def undo_proposals(proposal_ids: Sequence[str]) -> list[dict[str, Any]]:
    return await tools.undo_proposals(proposal_ids, HOST)


async def revise_proposal(proposal_id: str, text: str) -> dict[str, Any]:
    return await tools.revise_proposal(proposal_id, text, HOST)
